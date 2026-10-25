"""Build the minimal relevant company context for classification.

Matching is transparent and data-driven: an exposure is only produced from a
value actually present in the profile or from a relationship returned by the
repository.  Missing fields are reported as gaps and are never treated as
negative facts.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..models import (
    CompanyContext,
    CompanyProfile,
    ExposureMatch,
    NewsProcessingResult,
    RelationType,
    Relationship,
)
from ..utils import normalize_text

# Bilingual topic lexicon used only to align news vocabulary with profile
# vocabulary.  It does not encode any judgement about a company.
TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    "gas": ("газ", "gas", "спг", "lng", "метан"),
    "oil": ("нефт", "oil", "нефтепродукт", "crude"),
    "coal": ("уголь", "coal"),
    "electricity": ("электроэнерг", "electricity", "электросет", "энергосистем"),
    "energy": ("энерг", "energy", "топлив", "fuel"),
    "metals": ("металл", "metal", "сталь", "steel", "алюмин", "aluminium", "aluminum"),
    "chemicals": ("хими", "chemical", "удобрен", "fertilizer"),
    "agriculture": ("сельск", "agricultur", "зерно", "grain", "урожай", "crop"),
    "logistics": ("логист", "logistic", "перевоз", "transport", "транспорт", "порт", "port", "судоход"),
    "prices": ("цена", "цен ", "цены", "стоимост", "price", "pricing", "подорож", "подешев", "дорожа"),
    "rates": ("ставк", "rate", "процент", "interest"),
    "currency": ("курс", "валют", "currency", "рубл", "доллар", "ruble", "dollar", "exchange"),
    "regulation": ("регулир", "закон", "пошлин", "тариф", "tariff", "regulation", "налог", "tax", "лиценз", "license"),
    "sanctions": ("санкц", "sanction", "ограничен", "embargo", "эмбарго"),
    "demand": ("спрос", "потреблен", "demand", "consumption", "продаж", "sales"),
    "supply": ("поставк", "supply", "добыч", "production", "дефицит", "shortage", "мощност", "capacity"),
    "contract": ("контракт", "contract", "сделк", "договор", "agreement", "соглашен"),
    "it": ("it", "ит-", "software", "софт", "технолог", "technology", "digital", "цифров"),
    "finance": ("банк", "bank", "кредит", "credit", "финанс", "finance", "инвест", "invest"),
    "construction": ("строит", "construction", "недвижим", "real estate", "девелоп"),
    "retail": ("ритейл", "retail", "магазин", "store", "торгов"),
    "healthcare": ("медицин", "healthcare", "фарм", "pharma", "здравоохран"),
    "aviation": ("авиа", "aviation", "самолет", "aircraft", "airline"),
}

# Words too generic to prove a link on their own.
_STOPWORDS = {
    "компания", "компании", "company", "рынок", "market", "россия", "russia",
    "российский", "russian", "крупный", "large", "новый", "new", "рост", "growth",
    "снижение", "decline", "год", "year", "млрд", "млн", "billion", "million",
    "который", "которые", "этот", "эта", "this", "that", "для", "при", "как",
    "или", "and", "the", "of", "на", "по", "в", "с", "и", "о",
}

_TOKEN_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9][a-zA-Zа-яА-ЯёЁ0-9\-]{2,}")

#: Direction lexicons.  Only explicit surface forms are used; the result is
#: reported as an event signal, never as a financial forecast.
_UP_TERMS = (
    "вырос", "рост", "увелич", "подорож", "повыс", "повышен", "дорожа",
    "increase", "rose", "rise", "grew", "grow", "higher", "up ",
)
_DOWN_TERMS = (
    "снизил", "снижен", "упал", "падени", "подешев", "сократ", "дешев",
    "decrease", "fell", "fall", "drop", "lower", "decline",
)


def _terms(value: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(normalize_text(value)) if t not in _STOPWORDS and len(t) >= 4}


def detect_topics(text: str) -> set[str]:
    normalized = normalize_text(text)
    if not normalized:
        return set()
    topics = set()
    for topic, terms in TOPIC_TERMS.items():
        for term in terms:
            if normalize_text(term) in normalized:
                topics.add(topic)
                break
    return topics


def detect_direction(text: str) -> str:
    normalized = normalize_text(text)
    up = any(term in normalized for term in _UP_TERMS)
    down = any(term in normalized for term in _DOWN_TERMS)
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return "unknown"


def _dedupe(exposures: Iterable[ExposureMatch]) -> list[ExposureMatch]:
    seen = set()
    result = []
    for exposure in exposures:
        key = (exposure.kind, exposure.field, exposure.matched_value.lower(), exposure.matched_term.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(exposure)
    return result


class CompanyContextBuilder:
    """Collect profile, relationships and explicit exposure candidates."""

    def __init__(self, *, max_relationships: int = 50) -> None:
        self.max_relationships = max_relationships

    def build(
        self,
        profile: CompanyProfile,
        processing: NewsProcessingResult,
        *,
        news_text: str = "",
        relationships: list[Relationship] | None = None,
    ) -> CompanyContext:
        relationships = list(relationships or [])[: self.max_relationships]
        full_text = " ".join(
            filter(
                None,
                [
                    news_text or "",
                    " ".join(processing.main_events),
                    " ".join(f"{f.subject} {f.action} {f.object or ''}" for f in processing.facts),
                ],
            )
        )
        news_topics = detect_topics(full_text)
        news_terms = _terms(full_text)
        gaps = self._gaps(profile, relationships)
        exposures = self._profile_exposures(profile, news_topics, news_terms)
        exposures.extend(self._relationship_exposures(relationships, full_text))
        exposures.extend(self._listed_counterparty_exposures(profile, full_text))
        return CompanyContext(
            profile=profile,
            relationships=relationships,
            exposures=_dedupe(exposures),
            gaps=gaps,
            news_facts=list(processing.facts),
            news_text=news_text,
        )

    # ------------------------------------------------------------------
    def _gaps(self, profile: CompanyProfile, relationships: list[Relationship]) -> list[str]:
        gaps = []
        if not profile.industry:
            gaps.append("не указана отрасль")
        if not profile.products_services and not profile.activities:
            gaps.append("не указаны продукты, услуги и виды деятельности")
        if not profile.cost_drivers and not profile.key_resources:
            gaps.append("не указаны факторы затрат и ключевые ресурсы")
        if not profile.geography:
            gaps.append("не указана география деятельности")
        if not relationships:
            gaps.append("нет данных о связях с контрагентами")
        if not profile.has_minimal_context():
            gaps.append("в профиле нет операционных данных")
        if profile.updated_at is None:
            gaps.append("неизвестна дата актуальности профиля")
        return gaps

    def _profile_exposures(
        self, profile: CompanyProfile, news_topics: set[str], news_terms: set[str]
    ) -> list[ExposureMatch]:
        fields = [
            ("revenue", "activities"),
            ("revenue", "products_services"),
            ("cost", "processes"),
            ("cost", "key_resources"),
            ("cost", "cost_drivers"),
            ("operations", "geography"),
            ("risk", "known_risks"),
        ]
        exposures: list[ExposureMatch] = []
        for kind, field in fields:
            for value in getattr(profile, field):
                value_topics = detect_topics(value)
                matched = value_topics & news_topics
                if matched:
                    exposures.append(
                        ExposureMatch(
                            kind=kind,
                            field=field,
                            matched_value=value,
                            matched_term=", ".join(sorted(matched)),
                            evidence=f"profile.{field}: {value}",
                        )
                    )
                    continue
                overlap = _terms(value) & news_terms
                if overlap:
                    exposures.append(
                        ExposureMatch(
                            kind=kind,
                            field=field,
                            matched_value=value,
                            matched_term=", ".join(sorted(overlap)),
                            evidence=f"profile.{field}: {value}",
                        )
                    )
        industry_text = " ".join(filter(None, [profile.industry, profile.subindustry]))
        if industry_text and detect_topics(industry_text) & news_topics:
            exposures.append(
                ExposureMatch(
                    kind="sector",
                    field="industry",
                    matched_value=industry_text,
                    matched_term=", ".join(sorted(detect_topics(industry_text) & news_topics)),
                    evidence=f"profile.industry: {industry_text}",
                )
            )
        return exposures

    def _relationship_exposures(self, relationships: list[Relationship], news_text: str) -> list[ExposureMatch]:
        normalized = normalize_text(news_text)
        exposures = []
        for rel in relationships:
            name = normalize_text(rel.counterparty_name)
            if not name or name not in normalized:
                continue
            if rel.relation_type == RelationType.PAYMENT_LINK:
                # A payment artifact is not evidence of a dependency.
                continue
            kind = {
                RelationType.SUPPLIER: "supply",
                RelationType.CUSTOMER: "demand",
                RelationType.CREDITOR: "finance",
                RelationType.DEBTOR: "finance",
            }.get(rel.relation_type, "operations")
            exposures.append(
                ExposureMatch(
                    kind=kind,
                    field="relationship",
                    matched_value=rel.counterparty_name,
                    matched_term=rel.counterparty_name,
                    evidence=(
                        f"relationship {rel.relationship_id} "
                        f"({rel.relation_type.value}, confirmed={rel.confirmed}, "
                        f"significance={rel.significance.value})"
                    ),
                    relationship_id=rel.relationship_id,
                )
            )
        return exposures

    def _listed_counterparty_exposures(self, profile: CompanyProfile, news_text: str) -> list[ExposureMatch]:
        normalized = normalize_text(news_text)
        exposures = []
        for kind, items in (("supply", profile.suppliers), ("demand", profile.customers)):
            for item in items:
                name = str(item.get("name") or item.get("counterparty_name") or "")
                if name and normalize_text(name) in normalized:
                    exposures.append(
                        ExposureMatch(
                            kind=kind,
                            field="profile_suppliers" if kind == "supply" else "profile_customers",
                            matched_value=name,
                            matched_term=name,
                            evidence=f"profile counterparty entry: {name} (not independently confirmed)",
                        )
                    )
        return exposures
