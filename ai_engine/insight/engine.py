"""Insight engine: evidence-based impact chains for one company.

The engine never invents data.  It combines already extracted facts, company
profile fields, confirmed counterparty edges, retrieved banking-analytics
snippets, existing calculations and user refinements.  All additional searches
are hard-limited by ``SearchBudget`` (maximum two per company analysis).
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from ..classification.classifier import CompanyNewsClassifier
from ..config import Settings
from ..errors import (
    AnalysisNotFoundError,
    CompanyNotFoundError,
    IntelError,
    LLMError,
    SchemaValidationError,
    SearchLimitExceeded,
    ToolUnavailableError,
)
from ..llm.client import LLMClient
from ..llm.prompts import PROMPT_VERSIONS, adaptation_prompts, insight_prompts
from ..llm.schemas import InsightResponse
from ..models import (
    AnalysisStatus,
    CompanyNewsAssessment,
    CompanyProfile,
    EvidenceLink,
    ImpactDirection,
    ImpactEvent,
    ImpactHorizon,
    ImpactType,
    InsightResult,
    NewsProcessingResult,
    ProcessingStatus,
    Refinement,
    RefinementKind,
    RelationType,
    RelevanceClass,
    Relationship,
)
from ..storage.base import (
    AnalyticsIndex,
    AnalyticsSnippet,
    CalculationRepository,
    CompanyRepository,
    ExternalSearchTool,
    IntelStore,
    NewsRepository,
    RelationshipRepository,
)
from ..utils import stable_id, utcnow_iso
from .tools import InsightTools, SearchBudget, ToolSearchResult
from .validation import validate_llm_insight

logger = logging.getLogger("sxsvn_intel.insight")


def _dedupe_strings(items: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _profile_gaps(profile: CompanyProfile) -> list[str]:
    gaps = []
    if not profile.industry:
        gaps.append("industry")
    if not profile.products_services and not profile.activities:
        gaps.append("products/services")
    if not profile.cost_drivers and not profile.key_resources:
        gaps.append("cost drivers")
    if not profile.geography:
        gaps.append("geography")
    if not profile.updated_at:
        gaps.append("profile freshness date")
    return gaps


def _parse_horizon(text: str) -> ImpactHorizon | None:
    normalized = (text or "").lower()
    if "краткосроч" in normalized or "short" in normalized:
        return ImpactHorizon.SHORT_TERM
    if "среднесроч" in normalized or "medium" in normalized:
        return ImpactHorizon.MEDIUM_TERM
    if "долгосроч" in normalized or "long" in normalized:
        return ImpactHorizon.LONG_TERM
    return None


class InsightEngine:
    def __init__(
        self,
        settings: Settings,
        store: IntelStore,
        companies: CompanyRepository,
        relationships: RelationshipRepository,
        *,
        news: NewsRepository | None = None,
        analytics: AnalyticsIndex | None = None,
        calculations: CalculationRepository | None = None,
        external_search: ExternalSearchTool | None = None,
        llm: LLMClient | None = None,
        classifier: CompanyNewsClassifier | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.companies = companies
        self.relationships = relationships
        self.news = news
        self.analytics = analytics
        self.calculations = calculations
        self.external_search = external_search
        self.llm = llm
        self.classifier = classifier
        self._logger = logger_ or logger

    # ------------------------------------------------------------------
    def analyze(
        self,
        company_id: str,
        event_ids: Sequence[str],
        *,
        include_review: bool = True,
        include_drop: bool = False,
        auto_classify: bool = False,
        save: bool = True,
    ) -> InsightResult:
        requested = [str(e) for e in event_ids if e]
        if not requested:
            raise ValueError("event_ids must contain at least one news id")
        requested = list(dict.fromkeys(requested))

        company = self.companies.get_company(company_id)
        if company is None:
            raise CompanyNotFoundError(f"company {company_id!r} not found")

        try:
            relationships = self.relationships.list_relationships(
                company_id, limit=self.settings.max_relationships
            )
            relationship_error = ""
        except ToolUnavailableError:
            relationships = []
            relationship_error = "counterparty relationship store is unavailable"
        except Exception as exc:  # defensive: a tool must not kill the analysis
            relationships = []
            relationship_error = f"counterparty relationship store failed: {type(exc).__name__}"

        budget = SearchBudget(min(self.settings.max_additional_searches, SearchBudget.MAX_ALLOWED))
        tools = InsightTools(
            store=self.store,
            companies=self.companies,
            relationships=self.relationships,
            analytics=self.analytics,
            calculations=self.calculations,
            external_search=self.external_search,
            budget=budget,
        )

        limitations: list[str] = []
        missing_data: list[str] = []
        unknowns: list[str] = []
        if relationship_error:
            limitations.append(relationship_error)
            missing_data.append("counterparty relationships")

        items: list[tuple[NewsProcessingResult, CompanyNewsAssessment, str]] = []
        unavailable_events: list[str] = []
        filtered_events: list[str] = []
        stale_or_missing: list[str] = []

        for news_id in requested:
            processing = self.store.get_processing_result(news_id)
            if processing is None:
                unavailable_events.append(news_id)
                stale_or_missing.append(f"{news_id}: no processing result")
                continue
            if processing.status != ProcessingStatus.PROCESSED:
                unavailable_events.append(news_id)
                stale_or_missing.append(
                    f"{news_id}: processing status is {processing.status.value}"
                )
                continue
            news_text = self._news_text(news_id, processing)
            assessment = self.store.get_assessment(news_id, company_id)
            stale = assessment is not None and (
                assessment.content_version != processing.content_version
                or assessment.profile_version != company.profile_version
            )
            if stale or (assessment is None and auto_classify):
                assessment = self._reassess(company, processing, news_text)
            if assessment is None:
                unavailable_events.append(news_id)
                stale_or_missing.append(f"{news_id}: no company assessment")
                continue
            if stale and assessment.content_version != processing.content_version:
                unavailable_events.append(news_id)
                stale_or_missing.append(f"{news_id}: stale assessment")
                continue
            if assessment.relevance_class == RelevanceClass.DROP and not include_drop:
                filtered_events.append(news_id)
                continue
            if assessment.relevance_class == RelevanceClass.REVIEW and not include_review:
                filtered_events.append(news_id)
                continue
            items.append((processing, assessment, news_text))

        # --- evidence-enriched deterministic events ----------------------
        events = [
            self._build_event(assessment, processing, relationships, news_text)
            for processing, assessment, news_text in items
        ]
        snippets_by_id: dict[str, AnalyticsSnippet] = {}
        calculations_by_id: dict[str, dict[str, Any]] = {}
        sources: list[dict[str, Any]] = [
            {"type": "company_profile", "id": company.company_id, "version": company.profile_version}
        ]

        calculations: list[dict[str, Any]] = []
        try:
            calculations = tools.get_calculations(company_id)
        except ToolUnavailableError:
            missing_data.append("prepared calculations")
        except Exception as exc:
            limitations.append(f"calculation tool failed: {type(exc).__name__}")

        for calculation in calculations:
            ref = str(
                calculation.get("calculation_id")
                or calculation.get("id")
                or calculation.get("name")
                or ""
            )
            if ref:
                calculations_by_id[ref] = calculation
                sources.append({"type": "calculation", "id": ref, "title": calculation.get("name", ref)})

        analytics_unavailable = False
        external_results: list[dict[str, Any]] = []
        if events:
            analytics_unavailable, external_results = self._run_searches(
                items, events, company, tools, budget, snippets_by_id, sources, limitations, unknowns
            )
            if analytics_unavailable:
                missing_data.append("banking analytics search")

        # Attach analytics/external evidence only to the event whose query
        # produced it (the search loop already did that).

        for index, event in enumerate(events):
            processing, assessment, _ = items[index]
            sources.append(
                {
                    "type": "news",
                    "id": processing.news_id,
                    "source_id": processing.source_id,
                    "version": processing.content_version,
                }
            )
            for evidence in assessment.evidence:
                sources.append(
                    {"type": "fact", "id": evidence.fact_id, "news_id": processing.news_id}
                )
            for link in event.evidence:
                if link.kind == "relationship":
                    sources.append(
                        {
                            "type": "relationship",
                            "id": link.ref_id,
                            "confirmed": link.confirmed,
                        }
                    )

        # --- optional LLM synthesis --------------------------------------
        llm_failed = False
        synthesis = None
        allows_llm = (
            bool(events)
            and self.settings.use_llm
            and self.llm is not None
            and not any(e.relevance_class == RelevanceClass.DROP for e in events)
        )
        if allows_llm:
            try:
                synthesis = self._llm_synthesis(
                    company=company,
                    events=events,
                    items=items,
                    relationships=relationships,
                    snippets=list(snippets_by_id.values()),
                    calculations=calculations,
                    budget=budget,
                )
            except (LLMError, SchemaValidationError) as exc:
                llm_failed = True
                limitations.append(f"LLM synthesis rejected or failed: {exc.message}")
                self._logger.warning(
                    "insight LLM synthesis failed company_id=%s error=%s", company_id, exc.message
                )
            except Exception as exc:  # pragma: no cover - defensive
                llm_failed = True
                limitations.append(f"LLM synthesis failed: {type(exc).__name__}")
                self._logger.warning("insight LLM synthesis crashed company_id=%s", company_id)

        if synthesis is not None:
            events = synthesis.events
            summary = synthesis.summary or self._default_summary(company, events)
            key_risks = synthesis.key_risks
            opportunities = synthesis.potential_opportunities
            unknowns = _dedupe_strings(unknowns + synthesis.unknowns)
            recommended = synthesis.recommended_checks
            limitations = _dedupe_strings(limitations + synthesis.limitations)
            missing_data = _dedupe_strings(missing_data + synthesis.missing_data)
        else:
            summary = self._default_summary(company, events)
            key_risks, opportunities = self._derive_risks_and_opportunities(events)
            recommended = self._default_checks(events, company, relationships)

        # --- status -------------------------------------------------------
        analytics_unavailable = analytics_unavailable or self.analytics is None
        if self.analytics is None:
            limitations.append(
                "banking analytics index is not configured; no banking analytics was used"
            )
            if "banking analytics search" not in missing_data:
                missing_data.append("banking analytics search")

        if not events:
            if unavailable_events:
                status = AnalysisStatus.FAILED
            else:
                status = AnalysisStatus.COMPLETED
                unknowns.append("none of the requested events passed the relevance filter")
        elif unavailable_events or analytics_unavailable or llm_failed:
            status = AnalysisStatus.PARTIAL
        else:
            status = AnalysisStatus.COMPLETED

        missing_data = _dedupe_strings(
            missing_data + [f"company profile: {gap}" for gap in _profile_gaps(company)]
        )
        unknowns = _dedupe_strings(
            unknowns
            + stale_or_missing
            + [f"{e.news_id}: {lim}" for e in events for lim in e.limitations]
        )
        limitations = _dedupe_strings(limitations)
        if filtered_events:
            summary += f" {len(filtered_events)} event(s) were skipped by the relevance filter."

        analysis_id = stable_id(
            "analysis",
            company.company_id,
            company.profile_version,
            ",".join(sorted(requested)),
            ",".join(sorted(a.assessment_id for _, a, _ in items)),
            "parent",
        )
        result = InsightResult(
            company_id=company.company_id,
            analysis_status=status,
            summary=summary,
            events=events,
            key_risks=_dedupe_strings(key_risks),
            potential_opportunities=_dedupe_strings(opportunities),
            unknowns=unknowns,
            recommended_checks=_dedupe_strings(recommended),
            sources_used=self._dedupe_sources(sources),
            additional_searches_used=budget.used,
            limitations=limitations,
            missing_data=missing_data,
            analysis_id=analysis_id,
            company_profile_version=company.profile_version,
            model_id=(self.llm.model_id if synthesis is not None and self.llm else ""),
            prompt_version=(PROMPT_VERSIONS["insight"] if synthesis is not None else "rule-engine.v1"),
            created_at=utcnow_iso(),
        )
        if save:
            self.store.save_analysis(result)
        self._logger.info(
            "insight complete company_id=%s events=%s status=%s searches=%s",
            company.company_id,
            len(events),
            status.value,
            budget.used,
        )
        return result

    # ------------------------------------------------------------------
    def _news_text(self, news_id: str, processing: NewsProcessingResult) -> str:
        if self.news is not None:
            try:
                record = self.news.get_news(news_id)
            except Exception as exc:
                self._logger.warning("news lookup failed news_id=%s error=%s", news_id, type(exc).__name__)
                record = None
            if record is not None:
                return record.full_text
        return " ".join(f.quote for f in processing.facts)

    def _reassess(
        self,
        company: CompanyProfile,
        processing: NewsProcessingResult,
        news_text: str,
    ) -> CompanyNewsAssessment | None:
        if self.classifier is None:
            return None
        assessment = self.classifier.classify(company, processing, source_text=news_text)
        self.store.save_assessment(assessment)
        return assessment

    def _run_searches(
        self,
        items: list[tuple[NewsProcessingResult, CompanyNewsAssessment, str]],
        events: list[ImpactEvent],
        company: CompanyProfile,
        tools: InsightTools,
        budget: SearchBudget,
        snippets_by_id: dict[str, AnalyticsSnippet],
        sources: list[dict[str, Any]],
        limitations: list[str],
        unknowns: list[str],
    ) -> tuple[bool, list[dict[str, Any]]]:
        analytics_unavailable = False
        external_results: list[dict[str, Any]] = []
        covered = 0
        for index, (processing, assessment, _) in enumerate(items):
            if budget.remaining <= 0:
                break
            event = events[index]
            query = self._search_query(company, processing, event)
            try:
                result = tools.search_analytics(query)
            except ToolUnavailableError as exc:
                analytics_unavailable = True
                limitations.append(str(exc.message))
                break
            except SearchLimitExceeded:
                break
            except Exception as exc:
                analytics_unavailable = True
                limitations.append(f"banking analytics search failed: {type(exc).__name__}")
                break
            covered += 1
            for snippet in result.analytics:
                snippets_by_id[snippet.snippet_id] = snippet
                sources.append(
                    {
                        "type": "bank_service_analytics",
                        "id": snippet.snippet_id,
                        "title": snippet.title,
                        "source": snippet.source,
                    }
                )
                event.evidence.append(
                    EvidenceLink(
                        kind="analytics",
                        ref_id=snippet.snippet_id,
                        quote=snippet.text[:300],
                        source_id=snippet.source,
                        supports=f"retrieved banking analytics relevant to query: {query}",
                        confirmed=True,
                    )
                )
            if result.analytics:
                continue
            if tools.external_search is not None and budget.remaining > 0:
                try:
                    external = tools.search_external(query)
                except (ToolUnavailableError, SearchLimitExceeded):
                    external = ToolSearchResult()
                except Exception as exc:
                    limitations.append(f"external search failed: {type(exc).__name__}")
                    external = ToolSearchResult()
                for item in external.external[:3]:
                    ref = str(item.get("url") or item.get("id") or item.get("title") or "")
                    if not ref:
                        continue
                    external_results.append(item)
                    sources.append(
                        {
                            "type": "external_search",
                            "id": ref,
                            "title": item.get("title", ""),
                        }
                    )
                    event.evidence.append(
                        EvidenceLink(
                            kind="external_search",
                            ref_id=ref,
                            quote=str(item.get("snippet") or item.get("title") or "")[:300],
                            source_id=ref,
                            supports="unverified external search result",
                            confirmed=False,
                        )
                    )
                    event.hypothesis = True
                    event.limitations.append("external search result is not verified against a primary source")
            if not result.analytics and not external_results:
                unknowns.append(f"no banking analytics found for query: {query}")

        if budget.used >= budget.limit and covered < len(items):
            limitations.append(
                f"additional search budget ({budget.limit}) was exhausted; "
                f"{len(items) - covered} event(s) were analyzed without new searches"
            )
        return analytics_unavailable, external_results

    @staticmethod
    def _search_query(company: CompanyProfile, processing: NewsProcessingResult, event: ImpactEvent) -> str:
        parts = [company.name]
        parts.extend(processing.main_events[:1])
        for fact in processing.facts[:2]:
            parts.append(f"{fact.subject} {fact.action} {fact.object or ''}".strip())
        if event.mechanism:
            parts.append(event.mechanism)
        query = " ".join(p for p in parts if p)
        return query[:240]

    def _build_event(
        self,
        assessment: CompanyNewsAssessment,
        processing: NewsProcessingResult,
        relationships: list[Relationship],
        news_text: str,
    ) -> ImpactEvent:
        fact_by_id = {f.fact_id: f for f in processing.facts}
        evidence: list[EvidenceLink] = []
        for item in assessment.evidence:
            fact = fact_by_id.get(item.fact_id)
            if fact is None:
                continue
            evidence.append(
                EvidenceLink(
                    kind="fact",
                    ref_id=fact.fact_id,
                    quote=item.quote or fact.quote,
                    source_id=item.source_id or fact.source_id,
                    supports=item.supports or "supports the company-specific conclusion",
                    confirmed=True,
                )
            )
        if not evidence:
            for fact_id in assessment.supporting_fact_ids[:3]:
                fact = fact_by_id.get(fact_id)
                if fact:
                    evidence.append(
                        EvidenceLink(
                            kind="fact",
                            ref_id=fact.fact_id,
                            quote=fact.quote,
                            source_id=fact.source_id,
                            supports="cited supporting fact",
                            confirmed=True,
                        )
                    )

        limitations = list(assessment.limitations)
        hypothesis = assessment.relevance_class == RelevanceClass.REVIEW
        text = f"{news_text} {' '.join(f'{f.subject} {f.action}' for f in processing.facts)}"
        normalized = text.lower()
        for rel in relationships:
            name = rel.counterparty_name.lower()
            if not name or name not in normalized:
                continue
            if rel.relation_type == RelationType.PAYMENT_LINK:
                limitations.append(
                    f"counterparty {rel.counterparty_name}: a payment link/single payment "
                    "is not evidence of a dependency"
                )
                continue
            strength = rel.dependency_evidence()
            if strength == "none":
                continue
            evidence.append(
                EvidenceLink(
                    kind="relationship",
                    ref_id=rel.relationship_id,
                    quote=rel.evidence[0] if rel.evidence else rel.counterparty_name,
                    source_id=rel.source,
                    supports=(
                        "confirmed counterparty dependency"
                        if strength == "confirmed"
                        else "reported counterparty connection; dependency not confirmed"
                    ),
                    confirmed=strength == "confirmed",
                )
            )
            if strength != "confirmed":
                hypothesis = True
                limitations.append(
                    f"counterparty dependency on {rel.counterparty_name} is not confirmed "
                    f"(relation={rel.relation_type.value}, significance={rel.significance.value})"
                )

        direction = assessment.impact_direction
        consequences = self._consequences(direction, assessment.impact_mechanism)
        chain = self._chain(processing, assessment)
        if assessment.relevance_class == RelevanceClass.KEEP and not evidence:
            limitations.append("KEEP conclusion has no usable evidence after validation")
            hypothesis = True
        if direction == ImpactDirection.UNKNOWN:
            hypothesis = True
        if any(e.kind in {"inference", "hypothesis"} for e in evidence):
            hypothesis = True

        return ImpactEvent(
            news_id=assessment.news_id,
            relevance_class=assessment.relevance_class,
            impact_direction=direction,
            impact_type=assessment.impact_type,
            mechanism=assessment.impact_mechanism,
            potential_consequences=consequences,
            evidence=evidence,
            limitations=_dedupe_strings(limitations),
            confidence=assessment.confidence,
            impact_horizon=assessment.impact_horizon,
            chain=chain,
            hypothesis=hypothesis,
        )

    @staticmethod
    def _chain(processing: NewsProcessingResult, assessment: CompanyNewsAssessment) -> list[str]:
        if processing.main_events:
            event_text = processing.main_events[0]
        elif processing.facts:
            fact = processing.facts[0]
            event_text = f"{fact.subject} {fact.action}"
        else:
            event_text = "event details unavailable"
        return [
            f"Event (source): {event_text}",
            f"Company link: {assessment.impact_mechanism or 'not established from available data'}",
            f"Mechanism and direction: {assessment.impact_type.value} / {assessment.impact_direction.value}",
            "Potential consequence: see potential_consequences (not a realized loss)",
        ]

    @staticmethod
    def _consequences(direction: ImpactDirection, mechanism: str) -> list[str]:
        mechanism_text = mechanism or "the identified exposure"
        if direction == ImpactDirection.POSITIVE:
            return [f"Potential positive effect through {mechanism_text}; effect size is not calculated."]
        if direction == ImpactDirection.NEGATIVE:
            return [f"Potential negative effect through {mechanism_text}; effect size is not calculated."]
        if direction == ImpactDirection.MIXED:
            return [
                f"Potential positive and negative effects through {mechanism_text}; "
                "their balance is not established."
            ]
        return ["Direction of the potential effect cannot be established from available data."]

    def _llm_synthesis(
        self,
        *,
        company: CompanyProfile,
        events: list[ImpactEvent],
        items: list[tuple[NewsProcessingResult, CompanyNewsAssessment, str]],
        relationships: list[Relationship],
        snippets: list[AnalyticsSnippet],
        calculations: list[dict[str, Any]],
        budget: SearchBudget,
    ):
        facts_by_news = {
            processing.news_id: {f.fact_id: f for f in processing.facts}
            for processing, _, _ in items
        }
        analytics_by_id = {s.snippet_id: s for s in snippets}
        calculations_by_id = {
            str(c.get("calculation_id") or c.get("id") or c.get("name")): c
            for c in calculations
            if c.get("calculation_id") or c.get("id") or c.get("name")
        }
        system_prompt, user_prompt = insight_prompts(
            company=company.to_dict(),
            events=[e.to_dict() for e in events],
            relationships=[r.to_dict() for r in relationships],
            analytics=[s.to_dict() for s in snippets],
            calculations=calculations,
            additional_searches_left=budget.remaining,
        )
        result = self.llm.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_version=PROMPT_VERSIONS["insight"],
            response_model=InsightResponse,
        )
        return validate_llm_insight(
            result.content,
            company_id=company.company_id,
            requested_news_ids=[e.news_id for e in events],
            facts_by_news=facts_by_news,
            relationships=relationships,
            analytics_by_id=analytics_by_id,
            calculations_by_id=calculations_by_id,
        )

    @staticmethod
    def _default_summary(company: CompanyProfile, events: list[ImpactEvent]) -> str:
        if not events:
            return f"No relevant events were available for {company.name}."
        keep = sum(1 for e in events if e.relevance_class == RelevanceClass.KEEP)
        review = len(events) - keep
        return (
            f"Analyzed {len(events)} event(s) for {company.name}: "
            f"KEEP={keep}, REVIEW={review}. Conclusions are potential effects, "
            "not realized losses; see evidence and limitations per event."
        )

    @staticmethod
    def _derive_risks_and_opportunities(events: list[ImpactEvent]) -> tuple[list[str], list[str]]:
        risks = []
        opportunities = []
        for event in events:
            label = event.mechanism or "impact mechanism not established"
            if event.impact_direction in {ImpactDirection.NEGATIVE, ImpactDirection.MIXED}:
                risks.append(f"{event.news_id}: {label} (potential)")
            if event.impact_direction in {ImpactDirection.POSITIVE, ImpactDirection.MIXED}:
                opportunities.append(f"{event.news_id}: {label} (potential)")
        return risks, opportunities

    @staticmethod
    def _default_checks(
        events: list[ImpactEvent], company: CompanyProfile, relationships: list[Relationship]
    ) -> list[str]:
        checks = []
        if any(e.hypothesis for e in events):
            checks.append("Verify hypotheses against primary sources before acting")
        gaps = _profile_gaps(company)
        if gaps:
            checks.append("Complete company profile fields: " + ", ".join(gaps))
        unconfirmed = [r for r in relationships if r.dependency_evidence() == "hypothesis"]
        for rel in unconfirmed[:3]:
            checks.append(f"Confirm whether {rel.counterparty_name} is a material dependency")
        if not checks:
            checks.append("Re-check the event when a new source version or company profile is available")
        return checks

    @staticmethod
    def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen = set()
        result = []
        for source in sources:
            key = (source.get("type"), str(source.get("id")))
            if key in seen:
                continue
            seen.add(key)
            result.append(source)
        return result

    # ------------------------------------------------------------------
    def adapt(
        self,
        analysis_id: str,
        *,
        text: str,
        kind: RefinementKind | str = RefinementKind.OTHER,
        affected_event_ids: Sequence[str] | None = None,
        horizon: ImpactHorizon | str | None = None,
    ) -> InsightResult:
        if not text or not text.strip():
            raise ValueError("refinement text must not be empty")
        previous = self.store.get_analysis(analysis_id)
        if previous is None:
            raise AnalysisNotFoundError(f"analysis {analysis_id!r} not found")
        company = self.companies.get_company(previous.company_id)
        if company is None:
            raise CompanyNotFoundError(f"company {previous.company_id!r} not found")

        try:
            refinement_kind = kind if isinstance(kind, RefinementKind) else RefinementKind(str(kind))
        except ValueError as exc:
            allowed = ", ".join(k.value for k in RefinementKind)
            raise ValueError(f"invalid refinement kind; allowed: {allowed}") from exc
        if isinstance(horizon, str):
            try:
                horizon_value = ImpactHorizon(horizon)
            except ValueError as exc:
                raise ValueError("invalid impact horizon") from exc
        else:
            horizon_value = horizon

        refinement = Refinement(
            refinement_id="",
            company_id=company.company_id,
            analysis_id=analysis_id,
            text=text.strip(),
            kind=refinement_kind,
            affected_event_ids=list(affected_event_ids or []),
            horizon=horizon_value,
            source="user_statement",
            confirmed_external=False,
        )
        updated = InsightResult.from_dict(previous.to_dict())
        affected = set(affected_event_ids or [e.news_id for e in updated.events])
        refinement.affected_event_ids = sorted(affected)
        self.store.save_refinement(refinement)
        before = {e.news_id: e.to_dict() for e in updated.events}
        limitations = []
        user_link = EvidenceLink(
            kind="user_statement",
            ref_id=refinement.refinement_id,
            quote=refinement.text[:300],
            source_id="user",
            supports="user-provided refinement; not an external confirmed fact",
            confirmed=False,
        )

        synthesized = None
        synthesis_error = None
        if (
            refinement_kind not in {RefinementKind.NOT_RELEVANT, RefinementKind.HORIZON}
            and self.settings.use_llm
            and self.llm is not None
        ):
            try:
                synthesized = self._llm_adapt(previous, refinement, company, affected)
            except (LLMError, SchemaValidationError) as exc:
                synthesis_error = exc.message
                limitations.append(f"LLM adaptation failed: {exc.message}")

        if synthesized is not None:
            synthesized_by_id = {event.news_id: event for event in synthesized.events}
            updated.events = [
                synthesized_by_id.get(event.news_id, event) for event in updated.events
            ]
            updated.summary = synthesized.summary or updated.summary
            updated.unknowns = _dedupe_strings(updated.unknowns + synthesized.unknowns)
            updated.recommended_checks = _dedupe_strings(
                updated.recommended_checks + synthesized.recommended_checks
            )
            updated.limitations = _dedupe_strings(updated.limitations + synthesized.limitations)

        for event in updated.events:
            if event.news_id not in affected:
                continue
            if not any(e.kind == "user_statement" and e.ref_id == user_link.ref_id for e in event.evidence):
                event.evidence.append(user_link)
            if refinement_kind == RefinementKind.NOT_RELEVANT:
                event.relevance_class = RelevanceClass.DROP
                event.impact_direction = ImpactDirection.UNKNOWN
                event.impact_type = ImpactType.UNKNOWN
                event.impact_horizon = ImpactHorizon.UNKNOWN
                event.mechanism = ""
                event.potential_consequences = []
                event.hypothesis = False
                event.limitations.append(
                    "event marked not relevant based on a user statement; not verified externally"
                )
            elif refinement_kind == RefinementKind.HORIZON:
                event.impact_horizon = (
                    horizon_value or _parse_horizon(refinement.text) or event.impact_horizon
                )
                event.limitations.append("impact horizon revised from a user statement")
            elif refinement_kind == RefinementKind.DEPENDENCY:
                if event.impact_type == ImpactType.UNKNOWN:
                    event.impact_type = ImpactType.INDIRECT
                event.hypothesis = True
                event.limitations.append(
                    "user asserts a dependency; it is not confirmed by external data"
                )
                limitations.append(
                    "dependency refinement is a user statement, not a confirmed external fact"
                )
            elif synthesized is None:
                event.hypothesis = True
                event.limitations.append(
                    "analysis revised after a user refinement; affected by unverified user input"
                )
                limitations.append(
                    "user refinement applied; conclusions depending on it require external verification"
                )

        updated.analysis_id = stable_id(
            "analysis",
            company.company_id,
            updated.company_profile_version,
            ",".join(sorted(e.news_id for e in updated.events)),
            previous.analysis_id,
            refinement.refinement_id,
        )
        updated.parent_analysis_id = previous.analysis_id
        updated.refinements = list(previous.refinements) + [refinement.to_dict()]
        updated.created_at = utcnow_iso()
        updated.changed_event_ids = [
            news_id for news_id, data in before.items()
            if next((e.to_dict() for e in updated.events if e.news_id == news_id), None) != data
        ]
        if synthesized is None:
            updated.summary = (
                previous.summary
                + f" Refinement ({refinement_kind.value}) applied: {refinement.text[:200]}"
            )
        else:
            updated.summary += f" Refinement applied: {refinement.text[:200]}"
        updated.limitations = _dedupe_strings(
            list(previous.limitations)
            + (synthesized.limitations if synthesized is not None else [])
            + limitations
            + ["refinement was provided by the user and is not a confirmed external fact"]
        )
        risks, opportunities = self._derive_risks_and_opportunities(updated.events)
        updated.key_risks = risks
        updated.potential_opportunities = opportunities
        updated.unknowns = _dedupe_strings(
            list(previous.unknowns)
            + (synthesized.unknowns if synthesized is not None else [])
            + [e.limitations[-1] for e in updated.events if e.limitations and e.news_id in affected]
        )
        if synthesized is not None:
            updated.missing_data = _dedupe_strings(previous.missing_data + synthesized.missing_data)
        updated.recommended_checks = _dedupe_strings(
            list(previous.recommended_checks)
            + ["Verify refined conclusions against primary sources"]
        )
        updated.prompt_version = (
            PROMPT_VERSIONS["adaptation"] if synthesized is not None else "refinement-rules.v1"
        )
        updated.model_id = self.llm.model_id if synthesized is not None and self.llm else "rule-engine"
        if synthesis_error:
            updated.analysis_status = AnalysisStatus.PARTIAL
        self.store.save_analysis(updated)
        self._logger.info(
            "analysis adapted analysis_id=%s parent=%s refinement=%s",
            updated.analysis_id,
            previous.analysis_id,
            refinement.refinement_id,
        )
        return updated

    def _llm_adapt(
        self,
        previous: InsightResult,
        refinement: Refinement,
        company: CompanyProfile,
        affected: set[str],
    ):
        """Re-evaluate a refinement with the model, while grounding output in stored evidence."""
        events = [event for event in previous.events if event.news_id in affected]
        # The insight schema deliberately excludes DROP events. For an already
        # dropped event the deterministic path is more appropriate.
        if not events or any(e.relevance_class == RelevanceClass.DROP for e in events):
            return None
        processing_by_news = {
            event.news_id: self.store.get_processing_result(event.news_id) for event in events
        }
        if any(result is None for result in processing_by_news.values()):
            return None
        relationships = self.relationships.list_relationships(
            company.company_id, limit=self.settings.max_relationships
        )
        first = next(iter(processing_by_news.values()))
        context = self.classifier.builder.build(
            company,
            first,
            news_text=self._news_text(first.news_id, first),
            relationships=relationships,
        ) if self.classifier is not None else None
        if context is None:
            return None
        system_prompt, user_prompt = adaptation_prompts(previous, refinement, context)
        response = self.llm.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_version=PROMPT_VERSIONS["adaptation"],
            response_model=InsightResponse,
        )
        # The user statement is attached below with its own stored refinement
        # ID. Do not accept a model-invented user_statement reference.
        for raw_event in response.content.get("events", []):
            if isinstance(raw_event, dict):
                raw_event["evidence"] = [
                    item for item in raw_event.get("evidence", [])
                    if not isinstance(item, dict) or item.get("kind") != "user_statement"
                ]
        facts_by_news = {
            news_id: {fact.fact_id: fact for fact in result.facts}
            for news_id, result in processing_by_news.items()
        }
        old_evidence = {
            (event.news_id, evidence.ref_id): evidence
            for event in previous.events
            for evidence in event.evidence
            if evidence.kind == "analytics"
        }
        analytics_by_id = {
            ref_id: type("StoredSnippet", (), {"text": evidence.quote})()
            for (_, ref_id), evidence in old_evidence.items()
        }
        calculations_by_id = {
            str(source.get("id")): source
            for source in previous.sources_used
            if source.get("type") == "calculation"
        }
        return validate_llm_insight(
            response.content,
            company_id=company.company_id,
            requested_news_ids=[event.news_id for event in events],
            facts_by_news=facts_by_news,
            relationships=relationships,
            analytics_by_id=analytics_by_id,
            calculations_by_id=calculations_by_id,
        )
