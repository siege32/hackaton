"""Provider-independent validation of company-assessment output."""

from __future__ import annotations

from ..errors import SchemaValidationError
from ..models import (
    CompanyNewsAssessment,
    CompanyProfile,
    EvidenceItem,
    ImpactDirection,
    ImpactHorizon,
    ImpactType,
    NewsProcessingResult,
    RelevanceClass,
)
from ..utils import contains_quote, utcnow_iso


def _string_list(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return [str(value)]
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def _enum_or_error(enum_cls, value, field_name: str, *, required: bool = False, default=None):
    if value is None or value == "":
        if required:
            raise SchemaValidationError(f"missing required field {field_name!r}")
        return default
    try:
        return enum_cls(str(value))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_cls)
        raise SchemaValidationError(
            f"invalid {field_name} {value!r}; allowed values: {allowed}"
        ) from exc


def validate_assessment_payload(
    payload: dict,
    processing: NewsProcessingResult,
    profile: CompanyProfile,
    *,
    source_text: str = "",
    model_id: str = "",
    prompt_version: str = "",
    processing_version: str = "",
    classification_source: str = "llm",
) -> CompanyNewsAssessment:
    if not isinstance(payload, dict):
        raise SchemaValidationError("assessment payload must be a JSON object")

    payload_news = payload.get("news_id")
    if payload_news not in (None, "") and str(payload_news) != processing.news_id:
        raise SchemaValidationError(
            f"assessment news_id {payload_news!r} does not match requested {processing.news_id!r}"
        )
    payload_company = payload.get("company_id")
    if payload_company not in (None, "") and str(payload_company) != profile.company_id:
        raise SchemaValidationError(
            f"assessment company_id {payload_company!r} does not match requested {profile.company_id!r}"
        )

    relevance = _enum_or_error(
        RelevanceClass, payload.get("relevance_class"), "relevance_class", required=True
    )
    direction = _enum_or_error(
        ImpactDirection, payload.get("impact_direction"), "impact_direction",
        required=False, default=ImpactDirection.UNKNOWN,
    )
    impact_type = _enum_or_error(
        ImpactType, payload.get("impact_type"), "impact_type",
        required=False, default=ImpactType.UNKNOWN,
    )
    horizon = _enum_or_error(
        ImpactHorizon, payload.get("impact_horizon"), "impact_horizon",
        required=False, default=ImpactHorizon.UNKNOWN,
    )

    reasoning = str(payload.get("reasoning_summary") or "").strip()
    if not reasoning:
        raise SchemaValidationError("reasoning_summary is required for every assessment")

    warnings: list[str] = []
    limitations = _string_list(payload.get("limitations"))
    missing_data = _string_list(payload.get("missing_data"))
    facts_by_id = {f.fact_id: f for f in processing.facts}
    fact_text = "\n".join(f.quote for f in processing.facts)

    supporting_ids: list[str] = []
    for fact_id in _string_list(payload.get("supporting_fact_ids")):
        if fact_id in facts_by_id:
            if fact_id not in supporting_ids:
                supporting_ids.append(fact_id)
        else:
            warnings.append("Ссылка модели на несуществующий факт отброшена")

    evidence: list[EvidenceItem] = []
    seen_evidence = set()
    had_invalid_evidence = False
    raw_evidence = payload.get("evidence")
    if raw_evidence is not None and not isinstance(raw_evidence, list):
        raise SchemaValidationError("evidence must be a list")
    for index, item in enumerate(raw_evidence or []):
        if not isinstance(item, dict):
            warnings.append("Некорректное основание модели отброшено")
            had_invalid_evidence = True
            continue
        fact_id = str(item.get("fact_id") or "")
        fact = facts_by_id.get(fact_id)
        if fact is None:
            warnings.append("Ссылка модели на несуществующий факт отброшена")
            had_invalid_evidence = True
            continue
        quote = str(item.get("quote") or "").strip()
        if not quote:
            warnings.append("Основание модели без цитаты отброшено")
            had_invalid_evidence = True
            continue
        grounded = contains_quote(source_text, quote) or contains_quote(fact.quote, quote) or contains_quote(
            quote, fact.quote
        )
        if not grounded:
            warnings.append(
                "Цитата модели не найдена в тексте новости; основание отброшено"
            )
            had_invalid_evidence = True
            continue
        key = (fact_id, quote)
        if key in seen_evidence:
            continue
        seen_evidence.add(key)
        evidence.append(
            EvidenceItem(
                fact_id=fact_id,
                source_id=str(item.get("source_id") or fact.source_id),
                quote=quote,
                supports=str(item.get("supports") or "supports the conclusion"),
            )
        )
        if fact_id not in supporting_ids:
            supporting_ids.append(fact_id)

    if relevance == RelevanceClass.KEEP and not supporting_ids:
        warnings.append(
            "Оценка «Связано» понижена до «Нужна проверка»: нет подтверждающих фактов"
        )
        limitations.append("Модель указала связь без проверяемых ссылок на факты")
        relevance = RelevanceClass.REVIEW

    if relevance == RelevanceClass.KEEP and not evidence and had_invalid_evidence:
        warnings.append(
            "Оценка «Связано» понижена до «Нужна проверка»: цитаты не подтвердились текстом новости"
        )
        limitations.append("Цитаты модели не найдены в тексте новости")
        relevance = RelevanceClass.REVIEW

    if relevance == RelevanceClass.KEEP and not evidence and supporting_ids:
        # The model cited facts but omitted evidence entries; build them from
        # the actual fact rows.  This does not invent anything.
        for fact_id in supporting_ids[:5]:
            fact = facts_by_id[fact_id]
            evidence.append(
                EvidenceItem(
                    fact_id=fact_id,
                    source_id=fact.source_id,
                    quote=fact.quote,
                    supports="cited supporting fact",
                )
            )
        limitations.append("Основания восстановлены по цитатам указанных фактов")

    if relevance == RelevanceClass.DROP:
        direction = ImpactDirection.UNKNOWN
        impact_type = ImpactType.UNKNOWN
        horizon = ImpactHorizon.UNKNOWN

    confidence = payload.get("confidence")
    if confidence is not None:
        try:
            numeric = float(confidence)
        except (TypeError, ValueError):
            warnings.append("Уверенность модели не число и не учитывается")
            confidence = None
        else:
            clamped = max(0.0, min(1.0, numeric))
            if abs(clamped - numeric) > 1e-9:
                warnings.append("Уверенность модели вне диапазона 0–100% и приведена к границе")
            confidence = clamped
            limitations.append(
                "Уверенность — предварительная оценка модели, а не вероятность финансовых потерь"
            )

    if warnings:
        limitations.extend(w for w in warnings if w not in limitations)

    return CompanyNewsAssessment(
        news_id=processing.news_id,
        company_id=profile.company_id,
        relevance_class=relevance,
        impact_direction=direction,
        impact_type=impact_type,
        impact_mechanism=str(payload.get("impact_mechanism") or ""),
        impact_horizon=horizon,
        confidence=confidence,
        reasoning_summary=reasoning,
        supporting_fact_ids=supporting_ids,
        evidence=evidence,
        limitations=limitations,
        missing_data=missing_data,
        content_version=processing.content_version,
        profile_version=profile.profile_version,
        model_id=model_id,
        prompt_version=prompt_version,
        processing_version=processing_version,
        classification_source=classification_source,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
