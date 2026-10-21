"""Programmatic validation of the final analysis result."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .agent_tools import tool_names
from .models import AnalysisContext, AnalysisResult


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str


def _valid_date(value: str) -> bool:
    try:
        if "T" in value or " " in value:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def _check_date(value: str | None, field: str, issues: list[ValidationIssue]) -> None:
    if value and not _valid_date(value):
        issues.append(ValidationIssue("invalid_date", f"{field} is not an ISO date: {value}"))


def _check_fact_value(fact, issues: list[ValidationIssue]) -> None:
    if not isinstance(fact.value, dict):
        return
    amount = fact.value.get("amount")
    if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0):
        issues.append(ValidationIssue("invalid_value", f"fact {fact.fact_id} has a non-numeric amount"))
    unit = fact.value.get("unit")
    if unit is not None and (not isinstance(unit, str) or not unit.strip()):
        issues.append(ValidationIssue("invalid_unit", f"fact {fact.fact_id} has an empty unit"))


def validate_result(
    result: AnalysisResult,
    context: AnalysisContext,
    *,
    allowed_sources: set[str] | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    declared_sources = {source.source_id for source in context.sources}
    fact_sources = {source_id for fact in context.event_facts for source_id in fact.source_ids}
    if context.event is not None:
        fact_sources.update(context.event.source_ids)
    if context.news is not None:
        fact_sources.add(context.news.source_id)
    if allowed_sources:
        fact_sources.update(allowed_sources)
    bank_sources = {item.source_id for item in context.bank_evidence}
    valid_sources = declared_sources or (fact_sources | bank_sources)

    if declared_sources:
        for source_id in fact_sources | bank_sources:
            if source_id not in declared_sources:
                issues.append(ValidationIssue("source_not_declared", source_id))

    known_facts = {fact.fact_id for fact in context.event_facts}
    evidence_ids: set[str] = set()
    for evidence in result.evidence:
        if evidence.evidence_id in evidence_ids:
            issues.append(ValidationIssue("duplicate_evidence_id", evidence.evidence_id))
        evidence_ids.add(evidence.evidence_id)
        if not evidence.quote.strip():
            issues.append(ValidationIssue("empty_quote", evidence.evidence_id))
        if evidence.kind in {"event", "bank"} and evidence.source_id not in valid_sources:
            issues.append(ValidationIssue("unknown_source", evidence.source_id))
        if evidence.fact_id and known_facts and evidence.fact_id not in known_facts:
            issues.append(ValidationIssue("unknown_fact_id", evidence.fact_id))

    claim_ids: set[str] = set()
    for claim in (*result.ai_implications, *result.risks, *result.opportunities):
        if claim.claim_id in claim_ids:
            issues.append(ValidationIssue("duplicate_claim_id", claim.claim_id))
        claim_ids.add(claim.claim_id)
        if not claim.evidence_ids:
            issues.append(ValidationIssue("claim_without_evidence", claim.claim_id))
        if not set(claim.evidence_ids) <= evidence_ids:
            issues.append(ValidationIssue("unknown_evidence", claim.claim_id))

    if result.event.get("event_id") != context.event_id:
        issues.append(ValidationIssue("event_id_mismatch", "result event_id differs from context"))

    _check_date(context.as_of_date, "as_of_date", issues)
    if context.event is not None:
        _check_date(context.event.occurred_at, "event.occurred_at", issues)
        _check_date(context.event.published_at, "event.published_at", issues)
    for fact in context.event_facts:
        _check_date(fact.occurred_at, f"fact.{fact.fact_id}.occurred_at", issues)
        _check_date(fact.published_at, f"fact.{fact.fact_id}.published_at", issues)
        _check_fact_value(fact, issues)

    allowed_tools = tool_names()
    for step in result.trace:
        if step.tool and step.tool not in allowed_tools:
            issues.append(ValidationIssue("unknown_tool", step.tool))

    return issues


def ensure_context(context: AnalysisContext) -> None:
    if context.company_id != context.profile.company_id:
        raise ValueError("company_id must match profile.company_id")
    if not context.event_id or not context.as_of_date:
        raise ValueError("event_id and as_of_date are required")
