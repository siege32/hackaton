from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from cachetools import TTLCache
from pydantic import ValidationError

from .agent_tools import AgentToolkit, UnavailableToolkit
from .config import get_settings
from .evidence import build_evidence, evidence_quality
from .facts import FactExtractionService, FactExtractor, HeuristicFactExtractor, LLMFactExtractor
from .llm import LLMClient, LLMError
from .merging import merge_extractions, merge_facts, merge_missing, union
from .models import (
    AnalysisContext,
    AnalysisResult,
    EventExtraction,
    EventMeta,
    NewsItem,
    RelevanceAssessment,
)
from .prompts import analysis_messages
from .relevance import RelevanceAssessor, RuleBasedRelevanceAssessor
from .results import deterministic_result, no_link_result, result_from_payload
from .semantic import review_semantics
from .state import AnalysisState
from .validation import ensure_context, validate_result

logger = logging.getLogger(__name__)


class InsightEngine:
    """Bounded, evidence-first agent for one company/event analysis.

    State is explicit in ``AnalysisState``; the journal records every step and
    the search loop is limited to ``max_search_steps`` (0–2).
    """

    def __init__(
        self,
        llm: LLMClient | None = None,
        max_search_steps: int | None = None,
        *,
        llm_retries: int | None = None,
        toolkit: AgentToolkit | None = None,
        fact_extractor: FactExtractor | FactExtractionService | None = None,
        relevance_assessor: RelevanceAssessor | None = None,
    ):
        settings = get_settings()
        self.max_search_steps = settings.max_search_steps if max_search_steps is None else max_search_steps
        if self.max_search_steps < 0 or self.max_search_steps > 2:
            raise ValueError("max_search_steps must be between 0 and 2")
        self.llm_retries = settings.llm_retries if llm_retries is None else llm_retries
        if self.llm_retries < 0:
            raise ValueError("llm_retries must be non-negative")
        self.llm = llm
        self.toolkit: AgentToolkit = toolkit or UnavailableToolkit()
        self._max_input_chars = settings.llm_max_input_chars
        extractor = fact_extractor or (LLMFactExtractor(llm) if llm is not None else HeuristicFactExtractor())
        self._facts = extractor if isinstance(extractor, FactExtractionService) else FactExtractionService(extractor)
        self.relevance_assessor = relevance_assessor or RuleBasedRelevanceAssessor()
        self._cache: TTLCache[str, AnalysisResult] = TTLCache(
            maxsize=settings.cache_max_size,
            ttl=settings.cache_ttl_seconds,
        )

    # ------------------------------------------------------------------ API
    def analyze(self, context: AnalysisContext, *, use_cache: bool = True) -> AnalysisResult:
        ensure_context(context)
        cache_key = hashlib.sha256(
            json.dumps(context.to_dict(), sort_keys=True, default=str).encode()
        ).hexdigest()
        if use_cache and cache_key in self._cache:
            logger.info("analysis_cache_hit", extra={"event_id": context.event_id, "company_id": context.company_id})
            return self._cache[cache_key]

        state = AnalysisState(context=context, trace_id=context.run_id or str(uuid.uuid4()))
        if context.clarification:
            state.step("clarification", "ok", detail=context.clarification[:200])

        self._extract(state)
        self._assess(state)
        self._collect_tools(state)
        self._search_loop(state)

        relevance = _require_relevance(state)
        if relevance.decision == "DROP":
            state.step("route", "drop", detail="no company-event link; no impact claims generated")
            result = no_link_result(state)
        elif self.llm is None:
            result = deterministic_result(state)
        else:
            result = self._model_result(state)

        result = self._review(result, state)
        self._cache[cache_key] = result
        return result

    def refine(self, context: AnalysisContext, clarification: str, *, use_cache: bool = False) -> AnalysisResult:
        """Re-run analysis with a clarification; dependent claims are updated.

        Offline mode updates limitations/notes instead of pretending to reason
        again; the LLM path receives the clarification in the prompt.
        """

        if not clarification.strip():
            raise ValueError("clarification must not be empty")
        updated = context.model_copy(update={"clarification": clarification.strip()})
        return self.analyze(updated, use_cache=use_cache)

    # ------------------------------------------------------------- extraction
    def _extract(self, state: AnalysisState) -> None:
        context = state.context
        base = context.event or EventMeta(event_id=context.event_id)
        facts = context.event_facts
        extractor = "provided"
        notes = list(base.unconfirmed)

        if context.news is not None:
            try:
                extracted = self._facts.extract(context.news, context.event_id)
            except (LLMError, ValidationError, ValueError, TypeError) as exc:
                logger.warning(
                    "fact_extraction_failed",
                    extra={"event_id": context.event_id, "error_type": type(exc).__name__},
                )
                extracted = HeuristicFactExtractor().extract(context.news, context.event_id)
                notes.append(f"llm_extraction_error: {type(exc).__name__}")
                extractor = "heuristic"
                state.step("extract", "partial", detail=f"fallback to heuristic: {type(exc).__name__}")
            else:
                extractor = extracted.extractor
                state.step("extract", "ok", detail=extractor)
            facts = merge_facts(extracted.facts, facts)
            base = EventMeta(
                event_id=context.event_id,
                topic=base.topic or extracted.topic,
                occurred_at=base.occurred_at or extracted.occurred_at,
                published_at=base.published_at or extracted.published_at,
                participants=union(base.participants, extracted.participants),
                industries=union(base.industries, extracted.industries),
                geographies=union(base.geographies, extracted.geographies),
                unconfirmed=union(base.unconfirmed, extracted.unconfirmed, tuple(notes)),
                source_ids=union(base.source_ids, extracted.source_ids),
            )
        else:
            state.step("extract", "ok", detail="context facts supplied by the caller")

        participants = union(base.participants, *(fact.participants for fact in facts))
        industries = union(base.industries, *(fact.industries for fact in facts))
        geographies = union(base.geographies, *(fact.geographies for fact in facts))
        state.extraction = EventExtraction(
            event_id=context.event_id,
            topic=base.topic or next((fact.topic for fact in facts if fact.topic), ""),
            occurred_at=base.occurred_at,
            published_at=base.published_at,
            participants=participants,
            industries=industries,
            geographies=geographies,
            unconfirmed=union(base.unconfirmed, *(fact.unconfirmed for fact in facts)),
            source_ids=union(base.source_ids, *(fact.source_ids for fact in facts)),
            facts=facts,
            extractor=extractor,  # type: ignore[arg-type]
        )

    def _assess(self, state: AnalysisState) -> None:
        extraction = _require_extraction(state)
        relevance = self.relevance_assessor.assess(state.context.profile, extraction)
        state.relevance = relevance
        state.step("relevance", "ok", detail=f"{relevance.decision}/{relevance.channel}")

    # ------------------------------------------------------------------ tools
    def _collect_tools(self, state: AnalysisState) -> None:
        context = state.context
        extraction = _require_extraction(state)
        relevance = _require_relevance(state)

        state.record_tool(self.toolkit.get_company_profile(context.company_id))

        event_context = {
            "event_id": context.event_id,
            "topic": extraction.topic,
            "participants": list(extraction.participants),
        }
        exposure_result = self.toolkit.get_counterparty_exposure(context.company_id, event_context)
        state.record_tool(exposure_result)

        topic = extraction.topic or " ".join(extraction.participants)
        if not topic and extraction.facts:
            topic = extraction.facts[0].topic or extraction.facts[0].text[:120]
        state.record_tool(
            self.toolkit.search_bank_reports(topic, context.profile.model_dump(mode="json"))
        )

        if exposure_result.status == "success" and exposure_result.data.get("rows"):
            row = dict(exposure_result.data["rows"][0])
            parameters = {
                "total_payments": context.calculations.get("total_outgoing_payments")
                or context.calculations.get("total_payments")
            }
            state.record_tool(self.toolkit.calculate_exposure(row, parameters))

        if relevance.decision == "KEEP":
            state.step("plan", "ok", detail=f"impact channel: {relevance.channel}")

    def _search_loop(self, state: AnalysisState) -> None:
        extraction = _require_extraction(state)
        relevance = _require_relevance(state)
        context = state.context
        budget = self.max_search_steps

        while budget > 0 and _needs_search(relevance):
            budget -= 1
            state.search_steps_used += 1
            query = " ".join(extraction.participants) or extraction.topic or context.event_id
            result = self.toolkit.search_news(
                query, period=None, filters={"company_id": context.company_id}
            )
            state.record_tool(result)
            if result.status == "success":
                for raw in result.data.get("items", [])[:1]:
                    try:
                        extra = self._facts.extract(NewsItem.model_validate(raw), context.event_id)
                    except (LLMError, ValidationError, ValueError, TypeError) as exc:
                        logger.warning(
                            "news_extraction_failed",
                            extra={"event_id": context.event_id, "error_type": type(exc).__name__},
                        )
                        continue
                    extraction = merge_extractions(extraction, extra)
                relevance = self.relevance_assessor.assess(context.profile, extraction)
                state.step("relevance_recheck", "ok", detail=f"{relevance.decision}/{relevance.channel}")
            if not _needs_search(relevance):
                break

        state.extraction = extraction
        state.relevance = relevance
        if _needs_search(relevance):
            state.limit_hit = True
            state.step(
                "search_limit",
                "partial",
                detail=f"max_search_steps={self.max_search_steps} reached",
            )

    # --------------------------------------------------------------- results
    def _model_result(self, state: AnalysisState) -> AnalysisResult:
        context = state.context
        extraction = _require_extraction(state)
        relevance = _require_relevance(state)
        evidence = build_evidence(extraction, state.tools, relevance, context.bank_evidence)
        messages = analysis_messages(
            context,
            extraction,
            relevance,
            [item.model_dump(mode="json") for item in evidence],
            [step.model_dump(mode="json") for step in state.journal],
            max_chars=self._max_input_chars,
        )
        errors: list[str] = []
        model_name = getattr(getattr(self.llm, "config", None), "llm_model", "custom")
        for attempt in range(self.llm_retries + 1):
            try:
                payload = self.llm.complete(messages)  # type: ignore[union-attr]
                result = result_from_payload(payload, state.trace_id)
                state.step("model", "ok", detail=f"{model_name} attempt={attempt + 1}")
                return result
            except LLMError as exc:
                logger.warning(
                    "llm_call_failed",
                    extra={"event_id": context.event_id, "error_type": type(exc).__name__},
                )
                errors.append(f"llm_error: {exc}")
                state.step("model", "error", detail=f"{model_name} call failed")
                break
            except (ValidationError, ValueError, TypeError, KeyError) as exc:
                logger.warning(
                    "llm_payload_rejected",
                    extra={"event_id": context.event_id, "error_type": type(exc).__name__},
                )
                errors.append(f"llm_error: {type(exc).__name__}")
                state.step("model", "partial", detail=f"invalid payload attempt={attempt + 1}")

        return deterministic_result(state, extra_missing=tuple(errors)).model_copy(
            update={"status": "needs_review"}
        )

    # ---------------------------------------------------------------- review
    def _review(self, result: AnalysisResult, state: AnalysisState) -> AnalysisResult:
        context = state.context
        extraction = _require_extraction(state)
        relevance = _require_relevance(state)
        all_claims = (*result.ai_implications, *result.risks, *result.opportunities)
        state.record_tool(self.toolkit.validate_evidence(all_claims, result.evidence))

        bank_search = state.tools.get("search_bank_reports")
        bank_documents = bank_search.data.get("documents", []) if bank_search is not None else []
        tool_sources = {
            str(document.get("source_id") or document.get("document_id"))
            for document in bank_documents
            if document.get("source_id") or document.get("document_id")
        }
        allowed_sources = set(extraction.source_ids) | {item.source_id for item in context.bank_evidence} | tool_sources
        issues = validate_result(result, context, allowed_sources=allowed_sources)
        issues.extend(review_semantics(result, context, extraction, relevance))
        state.step(
            "validate",
            "ok" if not issues else "partial",
            detail=f"{len(issues)} issue(s)",
        )

        update: dict[str, Any] = {
            "relevance": relevance.model_dump(mode="json"),
            "evidence_quality": evidence_quality(result.evidence, relevance),
            "clarification_applied": context.clarification,
        }
        if issues:
            update["status"] = "needs_review"
            update["missing_data"] = merge_missing(
                result.missing_data,
                *(f"{issue.code}: {issue.message}" for issue in issues),
            )
            update["next_check"] = result.next_check or "Review the flagged issues"
            state.step(
                "decision",
                "needs_review",
                detail=", ".join(sorted({issue.code for issue in issues})),
            )
        else:
            state.step(
                "decision",
                "ok" if result.status == "completed" else result.status,
                detail=f"status={result.status}",
            )

        update["trace"] = tuple(state.journal)
        return result.model_copy(update=update)


# --------------------------------------------------------------------- module


def _needs_search(relevance: RelevanceAssessment) -> bool:
    return relevance.decision == "REVIEW" and bool(relevance.missing_data)


def _require_extraction(state: AnalysisState) -> EventExtraction:
    if state.extraction is None:
        raise RuntimeError("extraction is not initialized")
    return state.extraction


def _require_relevance(state: AnalysisState) -> RelevanceAssessment:
    if state.relevance is None:
        raise RuntimeError("relevance is not initialized")
    return state.relevance
