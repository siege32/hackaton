"""Evidence assembly and evidence-quality scoring.

Evidence stays separate from attention priority: ``evidence_quality`` answers
"how well is this supported", while ``priority_score`` answers "how much should
an analyst pay attention".
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .models import (
    AnalysisContext,
    BankEvidence,
    Claim,
    EventExtraction,
    Evidence,
    RelevanceAssessment,
    ToolResult,
)


def build_evidence(
    extraction: EventExtraction,
    tools: dict[str, ToolResult],
    relevance: RelevanceAssessment,
    bank_evidence: tuple[BankEvidence, ...] = (),
) -> list[Evidence]:
    """Build the evidence catalog used by both deterministic and LLM paths."""

    evidence: list[Evidence] = []
    sourced = [fact for fact in extraction.facts if fact.source_ids]
    for index, fact in enumerate(sourced, 1):
        evidence.append(
            Evidence(
                evidence_id=f"ev-{index}",
                source_id=fact.source_ids[0],
                quote=fact.quote or fact.text,
                kind="event",
                fact_id=fact.fact_id,
            )
        )

    calculation = tools.get("calculate_exposure")
    if calculation is not None and calculation.data.get("amount") is not None:
        share = calculation.data.get("share")
        if share is not None:
            evidence_id = "calc-payment-share"
            quote = (
                f"Observed payments: {calculation.data['amount']} of "
                f"{calculation.data['total_payments']}; share {share:.2%} of observed payments. "
                "Not revenue share and not loss probability."
            )
        else:
            evidence_id = "calc-payment-amount"
            quote = (
                f"Observed payments: {calculation.data['amount']}; total outgoing payments are "
                "unavailable, so no share is calculated."
            )
        evidence.append(
            Evidence(
                evidence_id=evidence_id,
                source_id="calculation:payment-share",
                quote=quote,
                kind="calculation",
            )
        )

    for index, item in enumerate(bank_evidence, 1):
        evidence.append(
            Evidence(
                evidence_id=f"bank-{index}",
                source_id=item.source_id,
                quote=item.text,
                kind="bank",
            )
        )

    bank_result = tools.get("search_bank_reports")
    if relevance.decision in {"KEEP", "REVIEW"} and bank_result is not None and bank_result.status == "success":
        documents = bank_result.data.get("documents", [])
        for index, document in enumerate(documents, 1):
            source_id = str(document.get("source_id") or document.get("document_id") or f"bank-document-{index}")
            quote = str(document.get("excerpt") or document.get("text") or "").strip()
            if not quote:
                continue
            evidence.append(
                Evidence(
                    evidence_id=f"bank-tool-{index}",
                    source_id=source_id,
                    quote=quote,
                    kind="bank",
                )
            )
    return evidence


def exposure_claim(tools: dict[str, ToolResult], evidence: list[Evidence]) -> Claim | None:
    exposure = tools.get("get_counterparty_exposure")
    if exposure is None or exposure.status != "success" or not exposure.data.get("rows"):
        return None
    row = exposure.data["rows"][0]
    calculation = tools.get("calculate_exposure")
    evidence_ids = [item.evidence_id for item in evidence if item.kind in {"event", "calculation"}]
    if not evidence_ids:
        return None
    share = calculation.data.get("share") if calculation is not None else None
    counterparty = row.get("counterparty", "the counterparty")
    if share is not None:
        text = (
            f"Observed payments to {counterparty} represent {share:.2%} of observed outgoing payments; "
            "an outage at this counterparty could disrupt the company's operations."
        )
    else:
        text = (
            f"Payments to {counterparty} are observed; the total payment base is unknown, "
            "so no share is calculated."
        )
    return Claim(
        claim_id="counterparty-exposure",
        text=text,
        evidence_ids=tuple(evidence_ids),
        limitation=(
            "Payment share is not revenue share, payments do not prove supply dependency, "
            "and this is not a loss probability."
        ),
        claim_type="hypothesis",
        feature=f"observed payments to {counterparty}",
        mechanism="A counterparty service disruption can interrupt inbound supply or logistics.",
        consequence="Possible temporary operational disruption; financial impact is not quantified.",
    )


def bank_view(
    context: AnalysisContext, tools: dict[str, ToolResult]
) -> dict[str, Any]:
    bank_result = tools.get("search_bank_reports")
    return {
        "evidence": [item.model_dump(mode="json") for item in context.bank_evidence],
        "external_materials": bank_result.data.get("documents", []) if bank_result and bank_result.status == "success" else [],
        "search_status": bank_result.status if bank_result is not None else "empty",
    }


def tool_notes(
    context: AnalysisContext,
    relevance: RelevanceAssessment,
    tools: dict[str, ToolResult],
    limit_hit: bool,
) -> tuple[str, ...]:
    notes: list[str] = []
    for name, result in tools.items():
        if result.status == "error":
            notes.append(f"tool_error: {name}: {result.message}")

    if not context.bank_evidence:
        notes.append("bank_material_not_found: no verified Alfa-Bank material linked to this event")

    exposure_result = tools.get("get_counterparty_exposure")
    if (
        exposure_result is not None
        and exposure_result.status == "empty"
        and relevance.channel == "counterparty"
    ):
        notes.append(f"counterparty_exposure_empty: {exposure_result.message}")

    if limit_hit:
        notes.append("search_limit_reached: maximum extra search steps used")
    return tuple(notes)


def event_payload(context: AnalysisContext, extraction: EventExtraction) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_id": context.event_id,
        "facts": [fact.model_dump(mode="json") for fact in extraction.facts],
    }
    if extraction.topic:
        payload["topic"] = extraction.topic
    if extraction.occurred_at:
        payload["occurred_at"] = extraction.occurred_at
    if extraction.published_at:
        payload["published_at"] = extraction.published_at
    if extraction.participants:
        payload["participants"] = list(extraction.participants)
    if extraction.unconfirmed:
        payload["unconfirmed"] = list(extraction.unconfirmed)
    return payload


def evidence_quality(evidence: tuple[Evidence, ...], relevance: RelevanceAssessment) -> dict[str, Any]:
    counts = Counter(item.kind for item in evidence)
    has_bank = counts.get("bank", 0) > 0
    if has_bank and counts.get("event", 0) and counts.get("calculation", 0):
        level = "high"
    elif has_bank and counts.get("event", 0):
        level = "medium"
    elif counts.get("event", 0) and counts.get("calculation", 0):
        level = "medium"
    else:
        level = "low"
    return {
        "level": level,
        "counts": dict(counts),
        "has_bank_material": has_bank,
        "relevance_decision": relevance.decision,
        "note": "evidence quality is stored separately from attention priority",
    }
