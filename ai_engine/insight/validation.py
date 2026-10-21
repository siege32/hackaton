"""Validation of LLM synthesis for the insight engine.

Only evidence that references data actually supplied to the model is accepted.
Invalid synthesis triggers a deterministic fallback in the engine; no invented
sources are stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import GroundingValidationError, SchemaValidationError
from ..models import (
    EvidenceLink,
    ImpactDirection,
    ImpactEvent,
    ImpactHorizon,
    ImpactType,
    RelevanceClass,
)
from ..utils import contains_quote, utcnow_iso

_ALLOWED_EVIDENCE_KINDS = {
    "fact", "relationship", "analytics", "calculation", "inference", "hypothesis",
    "user_statement", "external_search",
}


@dataclass
class ValidatedSynthesis:
    summary: str
    events: list[ImpactEvent] = field(default_factory=list)
    key_risks: list[str] = field(default_factory=list)
    potential_opportunities: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    recommended_checks: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)


def _string_list(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return [str(value)]
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def _enum(enum_cls, value, field_name: str, default=None):
    if value in (None, ""):
        return default
    try:
        return enum_cls(str(value))
    except ValueError as exc:
        allowed = ", ".join(i.value for i in enum_cls)
        raise SchemaValidationError(f"invalid {field_name} {value!r}; allowed: {allowed}") from exc


def validate_llm_insight(
    payload: dict,
    *,
    company_id: str,
    requested_news_ids: list[str],
    facts_by_news: dict[str, dict],
    relationships: list,
    analytics_by_id: dict[str, object],
    calculations_by_id: dict[str, dict],
) -> ValidatedSynthesis:
    if not isinstance(payload, dict):
        raise SchemaValidationError("insight payload must be a JSON object")
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise SchemaValidationError("insight payload must contain an events list")

    relationships_by_id = {r.relationship_id: r for r in relationships}
    requested = set(requested_news_ids)
    events: list[ImpactEvent] = []
    seen_news: set[str] = set()

    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            raise SchemaValidationError(f"events[{index}] is not an object")
        news_id = str(raw.get("news_id") or "")
        if news_id not in requested:
            raise SchemaValidationError(
                f"events[{index}] references news {news_id!r} that was not requested"
            )
        if news_id in seen_news:
            raise SchemaValidationError(f"duplicate event for news {news_id!r}")
        seen_news.add(news_id)

        relevance = _enum(RelevanceClass, raw.get("relevance_class"), "relevance_class", RelevanceClass.REVIEW)
        if relevance == RelevanceClass.DROP:
            raise SchemaValidationError("DROP events must not be passed to synthesis")
        direction = _enum(ImpactDirection, raw.get("impact_direction"), "impact_direction", ImpactDirection.UNKNOWN)
        impact_type = _enum(ImpactType, raw.get("impact_type"), "impact_type", ImpactType.UNKNOWN)
        horizon = _enum(ImpactHorizon, raw.get("impact_horizon"), "impact_horizon", ImpactHorizon.UNKNOWN)

        evidence = []
        raw_evidence = raw.get("evidence") or []
        if not isinstance(raw_evidence, list):
            raise SchemaValidationError(f"events[{index}].evidence must be a list")
        for e_index, item in enumerate(raw_evidence):
            if not isinstance(item, dict):
                raise SchemaValidationError(f"events[{index}].evidence[{e_index}] is not an object")
            kind = str(item.get("kind") or "").strip()
            ref_id = str(item.get("ref_id") or "").strip()
            if kind not in _ALLOWED_EVIDENCE_KINDS:
                raise SchemaValidationError(
                    f"events[{index}].evidence[{e_index}] has unknown kind {kind!r}"
                )
            quote = str(item.get("quote") or "")
            supports = str(item.get("supports") or "")
            source_id = str(item.get("source_id") or "")
            confirmed = bool(item.get("confirmed", True))

            if kind == "fact":
                fact = facts_by_news.get(news_id, {}).get(ref_id)
                if fact is None:
                    raise GroundingValidationError(
                        f"events[{index}] cites unknown fact {ref_id!r} for news {news_id!r}"
                    )
                if quote and not (contains_quote(fact.quote, quote) or contains_quote(quote, fact.quote)):
                    raise GroundingValidationError(
                        f"events[{index}] fact quote does not match fact {ref_id!r}"
                    )
                quote = quote or fact.quote
                source_id = source_id or fact.source_id
                confirmed = True
            elif kind == "relationship":
                rel = relationships_by_id.get(ref_id)
                if rel is None:
                    raise GroundingValidationError(
                        f"events[{index}] cites unknown relationship {ref_id!r}"
                    )
                confirmed = rel.dependency_evidence() == "confirmed"
                if not quote:
                    quote = rel.evidence[0] if rel.evidence else f"{rel.counterparty_name} ({rel.relation_type.value})"
            elif kind == "analytics":
                snippet = analytics_by_id.get(ref_id)
                if snippet is None:
                    raise GroundingValidationError(
                        f"events[{index}] cites analytics snippet {ref_id!r} that was not retrieved"
                    )
                if not quote:
                    quote = getattr(snippet, "text", "")[:300]
                confirmed = True
            elif kind == "calculation":
                calculation = calculations_by_id.get(ref_id)
                if calculation is None:
                    raise GroundingValidationError(
                        f"events[{index}] cites unknown calculation {ref_id!r}"
                    )
                confirmed = True
            elif kind in {"inference", "hypothesis"}:
                confirmed = False
            elif kind == "user_statement":
                confirmed = False
            elif kind == "external_search":
                raise GroundingValidationError(
                    f"events[{index}] cites external search data that was not supplied for synthesis"
                )

            evidence.append(
                EvidenceLink(
                    kind=kind,
                    ref_id=ref_id,
                    quote=quote,
                    source_id=source_id,
                    supports=supports,
                    confirmed=confirmed,
                )
            )

        if relevance == RelevanceClass.KEEP and not evidence:
            raise GroundingValidationError(
                f"events[{index}] claims KEEP without any evidence"
            )

        event = ImpactEvent(
            news_id=news_id,
            relevance_class=relevance,
            impact_direction=direction,
            impact_type=impact_type,
            mechanism=str(raw.get("mechanism") or ""),
            potential_consequences=_string_list(raw.get("potential_consequences")),
            evidence=[
                EvidenceLink(
                    kind=e.kind,
                    ref_id=e.ref_id,
                    quote=e.quote,
                    source_id=e.source_id,
                    supports=e.supports,
                    confirmed=e.confirmed,
                )
                for e in evidence
            ],
            limitations=_string_list(raw.get("limitations")),
            confidence=raw.get("confidence"),
            impact_horizon=horizon,
            chain=_string_list(raw.get("chain")),
            hypothesis=bool(raw.get("hypothesis", False))
            or relevance == RelevanceClass.REVIEW
            or direction == ImpactDirection.UNKNOWN,
        )
        events.append(event)

    if seen_news != requested:
        missing = sorted(requested - seen_news)
        raise SchemaValidationError(f"insight synthesis omitted events: {missing}")

    return ValidatedSynthesis(
        summary=str(payload.get("summary") or ""),
        events=events,
        key_risks=_string_list(payload.get("key_risks")),
        potential_opportunities=_string_list(payload.get("potential_opportunities")),
        unknowns=_string_list(payload.get("unknowns")),
        recommended_checks=_string_list(payload.get("recommended_checks")),
        limitations=_string_list(payload.get("limitations")),
        missing_data=_string_list(payload.get("missing_data")),
    )
