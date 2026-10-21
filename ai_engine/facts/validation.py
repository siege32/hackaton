"""Provider-independent validation of stage-A model output.

Validation is intentionally strict about grounding: a fact without a verbatim
source quote is rejected and reported, never silently repaired.  Numerical
values are taken from the source ``raw`` representation when possible so the
model cannot alter figures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..errors import GroundingValidationError, SchemaValidationError
from ..models import (
    Certainty,
    FactOrigin,
    NewsEntities,
    NewsFact,
    NewsProcessingResult,
    NewsRecord,
    Quantity,
    RelevanceClass,
)
from ..utils import contains_quote, normalize_text, utcnow_iso

_NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


@dataclass
class FactValidationOutcome:
    facts: list[NewsFact]
    entities: NewsEntities
    event_type: str
    main_events: list[str]
    general_relevance: RelevanceClass
    classification_reason: str
    uncertainties: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejected_facts: int = 0
    text_truncated: bool = False


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _string_list(value) -> list[str]:
    result = []
    for item in _as_list(value):
        if item is None:
            continue
        if isinstance(item, dict):
            # Accept common shapes like {"name": "..."}.
            item = item.get("name") or item.get("text") or item.get("value")
        if item is not None and str(item).strip():
            result.append(str(item).strip())
    return result


def _parse_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "да"}
    return default


def _parse_quantity(raw: dict, warnings: list[str], index: int) -> Quantity | None:
    raw_text = str(raw.get("raw") or "").strip()
    if not raw_text:
        warnings.append(f"fact[{index}] quantity without raw source text was dropped")
        return None
    parsed_value = None
    match = _NUM_RE.search(raw_text.replace(" ", ""))
    if match:
        try:
            parsed_value = float(match.group(0).replace(",", "."))
        except ValueError:
            parsed_value = None
    model_value = raw.get("value")
    if model_value is not None:
        try:
            model_numeric = float(model_value)
        except (TypeError, ValueError):
            warnings.append(f"fact[{index}] quantity value is not numeric and was ignored")
        else:
            if parsed_value is not None and abs(model_numeric - parsed_value) > 1e-9:
                warnings.append(
                    f"fact[{index}] quantity value {model_numeric} disagreed with source "
                    f"'{raw_text}'; the source value {parsed_value} was kept"
                )
    unit = raw.get("unit")
    return Quantity(
        value=parsed_value,
        unit=str(unit) if unit not in (None, "") else None,
        raw=raw_text,
    )


def validate_facts_payload(
    payload: dict,
    news: NewsRecord,
    *,
    text_truncated: bool = False,
) -> FactValidationOutcome:
    if not isinstance(payload, dict):
        raise SchemaValidationError("facts payload must be a JSON object")
    if not isinstance(payload.get("facts"), list):
        raise SchemaValidationError("facts payload must contain a 'facts' list")

    relevance_raw = payload.get("general_relevance")
    try:
        relevance = RelevanceClass(str(relevance_raw))
    except (ValueError, TypeError) as exc:
        raise SchemaValidationError(
            f"invalid general_relevance {relevance_raw!r}; expected KEEP/DROP/REVIEW"
        ) from exc

    warnings: list[str] = []
    if text_truncated:
        warnings.append("news text was truncated before fact extraction")
    source_text = news.full_text
    facts: list[NewsFact] = []
    rejected = 0
    seen: set[tuple] = set()

    for index, raw_fact in enumerate(payload["facts"]):
        if not isinstance(raw_fact, dict):
            rejected += 1
            warnings.append(f"fact[{index}] is not an object and was rejected")
            continue
        subject = str(raw_fact.get("subject") or "").strip()
        action = str(raw_fact.get("action") or "").strip()
        quote = str(raw_fact.get("quote") or "").strip()
        if not subject or not action:
            rejected += 1
            warnings.append(f"fact[{index}] has no subject/action and was rejected")
            continue
        if not quote:
            rejected += 1
            warnings.append(f"fact[{index}] has no source quote and was rejected")
            continue
        if not contains_quote(source_text, quote):
            rejected += 1
            warnings.append(
                f"fact[{index}] quote is not present in the source text and was rejected"
            )
            continue

        origin_raw = raw_fact.get("origin", FactOrigin.SOURCE.value)
        try:
            origin = FactOrigin(str(origin_raw))
        except ValueError:
            origin = FactOrigin.SOURCE
            warnings.append(f"fact[{index}] invalid origin {origin_raw!r}; treated as 'source'")
        certainty_raw = raw_fact.get("certainty", Certainty.ASSERTED.value)
        try:
            certainty = Certainty(str(certainty_raw))
        except ValueError:
            certainty = Certainty.UNCERTAIN
            warnings.append(
                f"fact[{index}] invalid certainty {certainty_raw!r}; treated as 'uncertain'"
            )

        quantities: list[Quantity] = []
        for q_index, raw_quantity in enumerate(_as_list(raw_fact.get("quantities"))):
            if not isinstance(raw_quantity, dict):
                warnings.append(f"fact[{index}] quantity[{q_index}] is not an object")
                continue
            quantity = _parse_quantity(raw_quantity, warnings, index)
            if quantity is not None:
                norm_raw = normalize_text(quantity.raw)
                if norm_raw and norm_raw not in normalize_text(quote):
                    warnings.append(
                        f"fact[{index}] quantity '{quantity.raw}' is not part of its quote and was dropped"
                    )
                    continue
                quantities.append(quantity)

        fact = NewsFact(
            fact_id="",
            news_id=news.news_id,
            content_version=news.content_version,
            source_id=news.source_id,
            fact_type=str(raw_fact.get("fact_type") or "other"),
            subject=subject,
            action=action,
            object=(str(raw_fact["object"]) if raw_fact.get("object") not in (None, "") else None),
            date=(str(raw_fact["date"]) if raw_fact.get("date") not in (None, "") else None),
            period=(str(raw_fact["period"]) if raw_fact.get("period") not in (None, "") else None),
            quantities=quantities,
            conditions=_string_list(raw_fact.get("conditions")),
            negated=_parse_bool(raw_fact.get("negated"), False),
            origin=origin,
            certainty=certainty,
            quote=quote,
            related_fact_ids=_string_list(raw_fact.get("related_fact_ids")),
            extra=dict(raw_fact.get("extra") or {}) if isinstance(raw_fact.get("extra"), dict) else {},
            created_at=utcnow_iso(),
        )
        fact.assign_stable_id()

        key = fact.stable_key()
        # ``seen`` already tracks each accepted stable key; rescanning
        # ``facts`` here made validation quadratic for large model outputs.
        if key in seen:
            warnings.append(f"fact[{index}] duplicates an earlier fact and was skipped")
            continue
        seen.add(key)
        facts.append(fact)

    if payload["facts"] and not facts:
        raise GroundingValidationError(
            "No extracted fact could be grounded in the source text",
            issues=warnings,
        )
    if not payload["facts"]:
        warnings.append("model returned no facts for a non-empty news item")

    valid_ids = {f.fact_id for f in facts}
    for fact in facts:
        if fact.related_fact_ids:
            fact.related_fact_ids = [fid for fid in fact.related_fact_ids if fid in valid_ids]

    entities_raw = payload.get("entities")
    entities = NewsEntities.from_dict(entities_raw) if isinstance(entities_raw, dict) else NewsEntities()

    return FactValidationOutcome(
        facts=facts,
        entities=entities,
        event_type=str(payload.get("event_type") or ""),
        main_events=_string_list(payload.get("main_events")),
        general_relevance=relevance,
        classification_reason=str(payload.get("classification_reason") or ""),
        uncertainties=_string_list(payload.get("uncertainties")),
        contradictions=_string_list(payload.get("contradictions")),
        missing_data=_string_list(payload.get("missing_information")),
        warnings=warnings,
        rejected_facts=rejected,
        text_truncated=text_truncated,
    )
