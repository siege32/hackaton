"""Result assembly without any model transport concerns."""

from __future__ import annotations

from typing import Any

from .evidence import bank_view, build_evidence, event_payload, exposure_claim, tool_notes
from .merging import merge_missing
from .models import AnalysisContext, AnalysisResult, Claim, EventExtraction, Evidence, RelevanceAssessment, Status
from .state import AnalysisState


def _state_parts(state: AnalysisState) -> tuple[AnalysisContext, EventExtraction, RelevanceAssessment]:
    if state.extraction is None or state.relevance is None:
        raise RuntimeError("analysis state is missing extraction or relevance")
    return state.context, state.extraction, state.relevance


def deterministic_result(
    state: AnalysisState,
    *,
    extra_missing: tuple[str, ...] = (),
) -> AnalysisResult:
    context, extraction, relevance = _state_parts(state)
    evidence = build_evidence(extraction, state.tools, relevance, context.bank_evidence)
    event_evidence_ids = tuple(item.evidence_id for item in evidence if item.kind == "event")

    claims: list[Claim] = [
        Claim(
            claim_id="implication-1",
            text="Event relevance requires analyst review of the observed company linkage.",
            evidence_ids=event_evidence_ids,
            limitation="No bank-specific causal evidence was supplied.",
            claim_type="hypothesis",
            feature="company-event linkage",
            mechanism="The event is linked to the company but the causal channel is not confirmed.",
            consequence="Impact is not quantified.",
        )
    ]
    risks: list[Claim] = []
    opportunities: list[Claim] = []

    exposure = exposure_claim(state.tools, evidence)
    if exposure is not None:
        claims.append(exposure)
        risks.append(
            exposure.model_copy(
                update={
                    "claim_id": "risk-counterparty-disruption",
                    "text": "A disruption at the observed counterparty could interrupt the company's operations.",
                    "consequence": "Possible temporary operational disruption; no loss amount is implied.",
                }
            )
        )

    if context.clarification:
        claims = [
            claim.model_copy(
                update={
                    "limitation": f"{claim.limitation} Clarification applied: {context.clarification}".strip(),
                    "revision_note": "revisited after user clarification",
                }
            )
            for claim in claims
        ]

    tool_errors = any(result.status == "error" for result in state.tools.values())
    status: Status = (
        "needs_review"
        if (not context.bank_evidence or tool_errors or bool(context.clarification))
        else "completed"
    )
    next_check = "Provide bank evidence"
    if context.bank_evidence:
        next_check = (
            "Confirm the company-event link and counterparty exposure"
            if relevance.decision == "REVIEW"
            else None
        )
    if context.clarification:
        next_check = "Confirm the revised hypothesis after clarification"

    return AnalysisResult(
        event=event_payload(context, extraction),
        priority={
            "priority_score": 0.5 if extraction.facts else 0.0,
            "meaning": "attention priority, not loss probability",
        },
        company_features=dict(context.profile.features),
        bank_view=bank_view(context, state.tools),
        ai_implications=tuple(claims),
        risks=tuple(risks),
        opportunities=tuple(opportunities),
        missing_data=merge_missing(
            context.missing_data,
            relevance.missing_data,
            extra_missing,
            tool_notes(context, relevance, state.tools, state.limit_hit),
        ),
        next_check=next_check,
        evidence=tuple(evidence),
        trace_id=state.trace_id,
        status=status,
    )


def no_link_result(state: AnalysisState) -> AnalysisResult:
    context, extraction, relevance = _state_parts(state)
    evidence = build_evidence(extraction, state.tools, relevance, context.bank_evidence)
    return AnalysisResult(
        event=event_payload(context, extraction),
        priority={
            "priority_score": 0.0,
            "meaning": "attention priority, not loss probability",
        },
        company_features=dict(context.profile.features),
        bank_view=bank_view(context, state.tools),
        ai_implications=(),
        risks=(),
        opportunities=(),
        missing_data=merge_missing(
            context.missing_data,
            relevance.missing_data,
            ("event_dropped: no evidence of company relevance",),
            tool_notes(context, relevance, state.tools, False),
        ),
        next_check="Provide a link between the event and the company",
        evidence=tuple(evidence),
        trace_id=state.trace_id,
        status="needs_review",
    )


def result_from_payload(payload: dict[str, Any], trace_id: str) -> AnalysisResult:
    if not isinstance(payload, dict):
        raise TypeError("model payload must be a JSON object")
    def claims(key: str) -> tuple[Claim, ...]:
        return tuple(Claim(**item) for item in payload.get(key, []))

    evidence = tuple(Evidence(**item) for item in payload.get("evidence", []))
    return AnalysisResult(
        event=payload["event"],
        priority=payload["priority"],
        company_features=payload.get("company_features", {}),
        bank_view=payload.get("bank_view", {}),
        ai_implications=claims("ai_implications"),
        risks=claims("risks"),
        opportunities=claims("opportunities"),
        missing_data=tuple(payload.get("missing_data", [])),
        next_check=payload.get("next_check"),
        evidence=evidence,
        trace_id=trace_id,
        status=payload.get("status", "needs_review"),
    )
