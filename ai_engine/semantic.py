"""Rule-based semantic review.

These checks complement the structural validation in ``validation.py``: they
look at the meaning of the result, not only at identifiers and schema.
"""

from __future__ import annotations

import json
from typing import Any

from .models import AnalysisContext, AnalysisResult, EventExtraction, RelevanceAssessment
from .validation import ValidationIssue


def _verified_fact_ids(extraction: EventExtraction) -> set[str]:
    return {fact.fact_id for fact in extraction.facts if fact.verified}


def _find_fact(extraction: EventExtraction, evidence) -> Any:
    for fact in extraction.facts:
        if evidence.fact_id and fact.fact_id == evidence.fact_id:
            return fact
        if (evidence.quote or "").strip() in {(fact.quote or fact.text).strip(), fact.text.strip()}:
            return fact
    return None


def review_semantics(
    result: AnalysisResult,
    context: AnalysisContext,
    extraction: EventExtraction,
    relevance: RelevanceAssessment,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    all_claims = (*result.ai_implications, *result.risks, *result.opportunities)
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    verified_ids = _verified_fact_ids(extraction)

    if relevance.decision == "DROP" and all_claims:
        issues.append(
            ValidationIssue(
                "claimed_impact_without_link",
                "relevance is DROP but the result still contains impact claims",
            )
        )

    for claim in all_claims:
        if claim.claim_type == "fact":
            for evidence_id in claim.evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None:
                    continue  # structural validation reports unknown evidence
                if evidence.kind == "hypothesis":
                    issues.append(
                        ValidationIssue(
                            "hypothesis_presented_as_fact",
                            f"claim {claim.claim_id} cites hypothesis evidence {evidence_id}",
                        )
                    )
                fact = _find_fact(extraction, evidence)
                if evidence.kind == "event" and fact is not None and fact.fact_id not in verified_ids:
                    issues.append(
                        ValidationIssue(
                            "unverified_fact_presented_as_fact",
                            f"claim {claim.claim_id} cites unverified fact {fact.fact_id}",
                        )
                    )
        if claim.claim_type in {"hypothesis", "implication"} and not claim.limitation.strip():
            issues.append(
                ValidationIssue(
                    "claim_without_limitation",
                    f"claim {claim.claim_id} must state its limitation",
                )
            )
        if claim.claim_type == "implication" and not (claim.mechanism.strip() and claim.consequence.strip()):
            issues.append(
                ValidationIssue(
                    "incomplete_causal_chain",
                    f"claim {claim.claim_id} must describe mechanism and consequence",
                )
            )

    seen_values: dict[str, set[str]] = {}
    for fact in extraction.facts:
        if not fact.topic or fact.value is None:
            continue
        seen_values.setdefault(fact.topic.casefold(), set()).add(
            json.dumps(fact.value, sort_keys=True, default=str)
        )
    for topic, values in seen_values.items():
        if len(values) > 1:
            issues.append(
                ValidationIssue(
                    "contradictory_sources",
                    f"contradictory sources for topic '{topic}'",
                )
            )

    if result.status == "needs_review" and not result.next_check and not result.missing_data:
        issues.append(
            ValidationIssue(
                "review_without_next_step",
                "needs_review requires next_check or an explicit missing-data note",
            )
        )

    return issues
