"""Company-event relevance assessment.

The rule-based assessor is the deterministic default. The LLM assessor is an
optional wrapper that falls back to the rules when the model is unavailable or
returns an invalid payload.

Rules follow the project constraint: an industry mismatch alone never drops an
event, because impact may travel through counterparties.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from .llm import LLMClient, LLMError
from .models import CompanyProfile, EventExtraction, RelevanceAssessment
from .prompts import relevance_messages

logger = logging.getLogger(__name__)


class RelevanceAssessor(Protocol):
    name: str

    def assess(self, profile: CompanyProfile, extraction: EventExtraction) -> RelevanceAssessment: ...


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", value.casefold())).strip()


def _matches(needle: str, haystack: tuple[str, ...]) -> bool:
    left = _norm(needle)
    if not left:
        return False
    for item in haystack:
        right = _norm(item)
        if not right:
            continue
        left_tokens = set(left.split())
        right_tokens = set(right.split())
        if left == right or (
            left_tokens
            and (left_tokens <= right_tokens or right_tokens <= left_tokens)
            and all(len(token) >= 4 for token in left_tokens & right_tokens)
        ):
            return True
    return False


def _overlap(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item for item in left if _matches(item, right))


class RuleBasedRelevanceAssessor:
    name = "rules"

    def assess(self, profile: CompanyProfile, extraction: EventExtraction) -> RelevanceAssessment:
        participants = extraction.participants
        industries = extraction.industries
        geographies = extraction.geographies

        if _matches(profile.company_id, participants) or _matches(profile.name, participants):
            return RelevanceAssessment(
                decision="KEEP",
                score=0.9,
                rationale="The company is a named participant of the event.",
                bases=("company is listed among event participants",),
                channel="direct",
            )

        counterparties = _overlap(participants, profile.counterparties)
        if counterparties:
            return RelevanceAssessment(
                decision="KEEP",
                score=0.7,
                rationale="A counterparty of the company is a named participant of the event.",
                bases=(f"counterparty mentioned: {', '.join(counterparties)}", "a payment link is not proof of supply dependency"),
                missing_data=("supply dependency and contract terms are not confirmed",),
                channel="counterparty",
            )

        industry_hits = _overlap(industries, profile.industries)
        geography_hits = _overlap(geographies, profile.geographies)

        if industry_hits:
            return RelevanceAssessment(
                decision="REVIEW",
                score=0.5,
                rationale="Only an industry overlap is visible; no direct or counterparty link was found.",
                bases=(f"industry overlap: {', '.join(industry_hits)}",),
                missing_data=("direct link or counterparty exposure is not confirmed",),
                channel="industry",
            )

        if geography_hits:
            return RelevanceAssessment(
                decision="REVIEW",
                score=0.45,
                rationale="Only a geography overlap is visible; no direct or counterparty link was found.",
                bases=(f"geography overlap: {', '.join(geography_hits)}",),
                missing_data=("direct link or counterparty exposure is not confirmed",),
                channel="geography",
            )

        if not participants and not industries and not geographies:
            return RelevanceAssessment(
                decision="REVIEW",
                score=0.35,
                rationale="Event metadata is insufficient to decide relevance.",
                bases=("participants, industries and geographies are unknown",),
                missing_data=("event participants, industries and geographies are unknown",),
                channel="none",
            )

        return RelevanceAssessment(
            decision="DROP",
            score=0.05,
            rationale="No company, counterparty, industry or geography connection was found.",
            bases=("no participant, counterparty, industry or geography match",),
            channel="none",
        )


class LLMRelevanceAssessor:
    """Optional model-based assessor with a deterministic fallback."""

    name = "llm"

    def __init__(self, llm: LLMClient, fallback: RelevanceAssessor | None = None):
        self.llm = llm
        self.fallback = fallback or RuleBasedRelevanceAssessor()

    def assess(self, profile: CompanyProfile, extraction: EventExtraction) -> RelevanceAssessment:
        try:
            payload = self.llm.complete(relevance_messages(profile.model_dump(mode="json"), extraction))
            return RelevanceAssessment.model_validate(payload)
        except (LLMError, ValueError, TypeError) as exc:
            logger.warning("relevance_model_failed", extra={"error_type": type(exc).__name__})
            return self.fallback.assess(profile, extraction)
