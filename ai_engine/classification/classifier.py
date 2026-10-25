"""Company-specific classification: LLM-first with a transparent rules fallback.

The rules engine exists for two reasons:
1. it can run offline and in tests without paid LLM calls;
2. it demonstrates that contextual relevance is derived from actual profile
   fields, not from the company name or industry alone.

LLM output that fails schema/grounding validation is never accepted silently:
it becomes an explicit ``REVIEW`` assessment with ``classification_source``
``llm_rejected`` and a limitation describing the failure.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..config import Settings
from ..errors import ConfigError, LLMError, SchemaValidationError
from ..llm.client import LLMClient
from ..llm.prompts import PROMPT_VERSIONS, company_assessment_prompts
from ..llm.schemas import AssessmentResponse
from ..models import (
    CompanyContext,
    CompanyNewsAssessment,
    CompanyProfile,
    EvidenceItem,
    ImpactDirection,
    ImpactHorizon,
    ImpactType,
    NewsProcessingResult,
    RelationType,
    Relationship,
    RelevanceClass,
    Significance,
)
from ..utils import contains_quote, normalize_text, utcnow_iso
from .context import CompanyContextBuilder, detect_direction
from .validation import validate_assessment_payload

logger = logging.getLogger("sxsvn_intel.classification")

RULE_PROMPT_VERSION = "rule-engine.v1"

_STRONG_KINDS = {"revenue", "cost", "supply", "demand", "risk", "finance"}


class RuleBasedClassifier:
    """Deterministic classifier over explicit profile exposures."""

    def __init__(self, builder: CompanyContextBuilder | None = None) -> None:
        self.builder = builder or CompanyContextBuilder()

    def classify(
        self,
        profile: CompanyProfile,
        processing: NewsProcessingResult,
        *,
        source_text: str = "",
        relationships: Sequence[Relationship] | None = None,
    ) -> CompanyNewsAssessment:
        relationships = list(relationships or [])
        text = source_text or " ".join(
            filter(None, [" ".join(processing.main_events), " ".join(f.quote for f in processing.facts)])
        )
        context = self.builder.build(profile, processing, news_text=text, relationships=relationships)

        if processing.general_relevance == RelevanceClass.DROP:
            return self._result(
                profile,
                processing,
                relevance=RelevanceClass.DROP,
                reason=(
                    "Новость не относится к деловой или экономической тематике: "
                    f"{processing.classification_reason or 'причина не указана'}"
                ),
                limitations=["Анализ для компании не проводился: новость отсеяна на этапе общей оценки"],
                missing_data=context.gaps,
                confidence=0.5,
            )

        if not processing.facts:
            return self._result(
                profile,
                processing,
                relevance=RelevanceClass.REVIEW,
                reason="Из новости не извлечено подтверждённых фактов, поэтому связь с компанией установить нельзя.",
                limitations=["нет извлечённых фактов, на которые мог бы опереться вывод о компании"],
                missing_data=context.gaps + ["подтверждённые факты из новости"],
                confidence=0.2,
            )

        if not profile.has_minimal_context():
            return self._result(
                profile,
                processing,
                relevance=RelevanceClass.REVIEW,
                reason=(
                    "В профиле компании нет операционных данных (деятельность, продукты, затраты, "
                    "география, контрагенты), поэтому уверенно оценить релевантность нельзя."
                ),
                limitations=["профиль компании слишком скудный для уверенного вывода"],
                missing_data=context.gaps,
                confidence=0.2,
            )

        strong = [e for e in context.exposures if self._is_strong(e, relationships)]
        weak = [e for e in context.exposures if e not in strong]
        # Prefer fact actions for direction: a news text may contain several
        # moves (e.g. price up and supply down), while the extracted event fact
        # identifies which change is actually being analysed.
        fact_direction_text = " ".join(
            f"{fact.action} {fact.quote}" for fact in processing.facts
        )
        event_direction = detect_direction(fact_direction_text)
        if event_direction == "unknown":
            event_direction = detect_direction(text)
        directions = [self._exposure_direction(e, event_direction) for e in strong]
        direction = self._combine_directions(directions)

        if not strong:
            if weak:
                return self._result(
                    profile,
                    processing,
                    relevance=RelevanceClass.REVIEW,
                    reason=(
                        "Найдена только слабая связь (совпадение отрасли, географии или неподтверждённый "
                        "контрагент); этого недостаточно, чтобы считать новость связанной с компанией."
                    ),
                    limitations=[
                        "совпадение отрасли или географии само по себе не доказывает существенную связь",
                        "зависимость от контрагента не подтверждена имеющимися данными",
                    ],
                    missing_data=context.gaps
                    + ["подтверждённая зависимость или операционная связь компании с событием"],
                    confidence=0.3,
                )
            return self._result(
                profile,
                processing,
                relevance=RelevanceClass.DROP,
                reason=(
                    "Не найдено подтверждённой связи события с данными профиля "
                    "(продукты, затраты, ресурсы, география, риски или подтверждённые контрагенты)."
                ),
                limitations=[
                    "отсутствие поля в профиле не доказывает, что зависимости нет",
                    "решение принято правилами только по имеющимся данным",
                ],
                missing_data=context.gaps,
                confidence=0.5,
            )

        keep = any(d != ImpactDirection.UNKNOWN for d in directions) or any(
            e.kind == "risk" for e in strong
        )
        if not keep:
            return self._result(
                profile,
                processing,
                relevance=RelevanceClass.REVIEW,
                reason=(
                    "Связь с деятельностью компании найдена, но направление влияния события "
                    "по тексту источника установить нельзя."
                ),
                limitations=["направление события в тексте явно не указано"],
                missing_data=context.gaps,
                confidence=0.3,
            )

        mechanism = self._mechanism(strong, processing, event_direction)
        direct = bool(profile.name) and normalize_text(profile.name) in normalize_text(text)
        impact_type = ImpactType.DIRECT if direct else ImpactType.INDIRECT
        limitations = [
            "оценка сделана правилами по имеющимся данным, без проверки моделью",
            "уверенность — предварительная эвристическая оценка, а не вероятность",
        ]
        unconfirmed = [e for e in context.exposures if e.relationship_id and not self._relationship_confirmed(e, relationships)]
        if unconfirmed:
            limitations.append("зависимость от контрагента не подтверждена и учитывается только как косвенный контекст")
        if any(e.field in {"profile_suppliers", "profile_customers"} for e in context.exposures):
            limitations.append("контрагенты из профиля не подтверждены независимыми источниками")

        evidence = self._evidence(strong, processing)
        return self._result(
            profile,
            processing,
            relevance=RelevanceClass.KEEP,
            reason=(
                f"Событие совпадает с явными факторами профиля компании: "
                f"{'; '.join(e.evidence for e in strong[:3])}. Механизм: {mechanism}"
            ),
            direction=direction,
            impact_type=impact_type,
            mechanism=mechanism,
            horizon=ImpactHorizon.UNKNOWN,
            limitations=limitations,
            missing_data=context.gaps,
            confidence=0.55,
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    def _is_strong(self, exposure, relationships: list[Relationship]) -> bool:
        if exposure.kind not in _STRONG_KINDS:
            return False
        if exposure.field in {"profile_suppliers", "profile_customers"}:
            return False
        if exposure.relationship_id:
            return self._relationship_confirmed(exposure, relationships)
        return True

    @staticmethod
    def _relationship_confirmed(exposure, relationships: list[Relationship]) -> bool:
        rel = next((r for r in relationships if r.relationship_id == exposure.relationship_id), None)
        if rel is None:
            return False
        return rel.dependency_evidence() == "confirmed"

    @staticmethod
    def _exposure_direction(exposure, event_direction: str) -> ImpactDirection:
        if event_direction == "unknown":
            return ImpactDirection.UNKNOWN
        up = event_direction == "up"
        if exposure.kind == "revenue":
            return ImpactDirection.POSITIVE if up else ImpactDirection.NEGATIVE
        if exposure.kind in {"cost", "finance"}:
            return ImpactDirection.NEGATIVE if up else ImpactDirection.POSITIVE
        if exposure.kind == "demand":
            return ImpactDirection.POSITIVE if up else ImpactDirection.NEGATIVE
        if exposure.kind == "risk":
            return ImpactDirection.NEGATIVE
        if exposure.kind == "supply":
            # A supplier-related increase is only interpretable as a cost
            # pressure when the event is about prices/rates/energy.
            return ImpactDirection.NEGATIVE if up else ImpactDirection.UNKNOWN
        return ImpactDirection.UNKNOWN

    @staticmethod
    def _combine_directions(directions: list[ImpactDirection]) -> ImpactDirection:
        values = {d for d in directions if d != ImpactDirection.UNKNOWN}
        if not values:
            return ImpactDirection.UNKNOWN
        if values == {ImpactDirection.POSITIVE}:
            return ImpactDirection.POSITIVE
        if values == {ImpactDirection.NEGATIVE}:
            return ImpactDirection.NEGATIVE
        return ImpactDirection.MIXED

    @staticmethod
    def _mechanism(strong, processing: NewsProcessingResult, event_direction: str) -> str:
        parts = []
        for exposure in strong[:3]:
            if exposure.kind in {"revenue", "demand"}:
                effect = "влияние на выручку и спрос"
            elif exposure.kind in {"cost", "finance"}:
                effect = "влияние на затраты"
            elif exposure.kind == "risk":
                effect = "известный риск компании"
            else:
                effect = "влияние на деятельность"
            parts.append(
                f"{effect} через «{exposure.matched_value}» "
                f"(совпадение: {exposure.matched_term})"
            )
        direction = {"up": "рост", "down": "снижение"}.get(event_direction, "изменение")
        return f"Событие ({direction}) может повлиять на компанию: " + "; ".join(parts)

    @staticmethod
    def _evidence(strong, processing: NewsProcessingResult) -> list[EvidenceItem]:
        terms = []
        for exposure in strong:
            terms.extend(t.strip() for t in exposure.matched_term.split(",") if t.strip())
        selected = []
        for fact in processing.facts:
            if any(contains_quote(fact.quote, term) or normalize_text(term) in normalize_text(fact.quote) for term in terms):
                selected.append(fact)
        if not selected:
            selected = processing.facts[:3]
        return [
            EvidenceItem(
                fact_id=fact.fact_id,
                source_id=fact.source_id,
                quote=fact.quote,
                supports=f"источник сообщает: {fact.subject} {fact.action}",
            )
            for fact in selected[:3]
        ]

    def _result(
        self,
        profile: CompanyProfile,
        processing: NewsProcessingResult,
        *,
        relevance: RelevanceClass,
        reason: str,
        direction: ImpactDirection = ImpactDirection.UNKNOWN,
        impact_type: ImpactType = ImpactType.UNKNOWN,
        mechanism: str = "",
        horizon: ImpactHorizon = ImpactHorizon.UNKNOWN,
        limitations: list[str] | None = None,
        missing_data: list[str] | None = None,
        confidence: float | None = None,
        evidence: list[EvidenceItem] | None = None,
    ) -> CompanyNewsAssessment:
        return CompanyNewsAssessment(
            news_id=processing.news_id,
            company_id=profile.company_id,
            relevance_class=relevance,
            impact_direction=direction,
            impact_type=impact_type,
            impact_mechanism=mechanism,
            impact_horizon=horizon,
            confidence=confidence,
            reasoning_summary=reason,
            supporting_fact_ids=[e.fact_id for e in (evidence or [])],
            evidence=evidence or [],
            limitations=limitations or [],
            missing_data=missing_data or [],
            content_version=processing.content_version,
            profile_version=profile.profile_version,
            model_id="rule-engine",
            prompt_version=RULE_PROMPT_VERSION,
            processing_version=processing.processing_version,
            classification_source="rules",
            created_at=utcnow_iso(),
            updated_at=utcnow_iso(),
        )


class CompanyNewsClassifier:
    """Selects LLM or rules mode and guarantees grounded, validated output."""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient | None = None,
        *,
        builder: CompanyContextBuilder | None = None,
        rules: RuleBasedClassifier | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.builder = builder or CompanyContextBuilder(max_relationships=settings.max_relationships)
        self.rules = rules or RuleBasedClassifier(self.builder)

    def classify(
        self,
        profile: CompanyProfile,
        processing: NewsProcessingResult,
        *,
        source_text: str = "",
        relationships: Sequence[Relationship] | None = None,
    ) -> CompanyNewsAssessment:
        relationships = list(relationships or [])
        if self.settings.classifier_mode == "llm" and self.llm is None:
            raise ConfigError("CLASSIFIER_MODE=llm but no LLM client is configured")
        use_llm = self.settings.use_llm and self.llm is not None

        if not use_llm or processing.general_relevance == RelevanceClass.DROP or not processing.facts:
            return self.rules.classify(
                profile, processing, source_text=source_text, relationships=relationships
            )

        context = self.builder.build(
            profile, processing, news_text=source_text, relationships=relationships
        )
        prompt_version = PROMPT_VERSIONS["company_assessment"]
        system_prompt, user_prompt = company_assessment_prompts(context, processing)
        logger.info(
            "classify company news_id=%s company_id=%s profile_version=%s prompt=%s",
            processing.news_id,
            profile.company_id,
            profile.profile_version,
            prompt_version,
        )
        response = self.llm.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_version=prompt_version,
            response_model=AssessmentResponse,
        )

        try:
            assessment = validate_assessment_payload(
                response.content,
                processing,
                profile,
                source_text=source_text,
                model_id=response.model_id,
                prompt_version=prompt_version,
                processing_version=self.settings.processing_version,
                classification_source="llm",
            )
        except SchemaValidationError as exc:
            logger.warning(
                "LLM assessment rejected news_id=%s company_id=%s error=%s",
                processing.news_id,
                profile.company_id,
                exc.message,
            )
            return self._rejected_assessment(
                profile,
                processing,
                context,
                reason=f"Ответ модели не прошёл проверку структуры и опоры на источник: {exc.message}",
                model_id=response.model_id,
                prompt_version=prompt_version,
            )

        # If the model itself says material data is missing, it cannot at the
        # same time conclude that the event is unrelated. Keep it for review.
        # The missing_data list here is the model's list; context gaps are
        # appended below and do not independently trigger this downgrade.
        if assessment.relevance_class == RelevanceClass.DROP and assessment.missing_data:
            assessment.relevance_class = RelevanceClass.REVIEW
            assessment.limitations.append(
                "DROP with missing company data was downgraded to REVIEW"
            )

        # Missing profile data is relevant context, not automatically a
        # negative fact. Keep it visible in the stored assessment.
        for gap in context.gaps:
            if gap not in assessment.missing_data:
                assessment.missing_data.append(gap)
        if assessment.relevance_class == RelevanceClass.KEEP and not assessment.impact_mechanism:
            assessment.relevance_class = RelevanceClass.REVIEW
            assessment.limitations.append(
                "KEEP without a described impact mechanism was downgraded to REVIEW"
            )
        return assessment

    def _rejected_assessment(
        self,
        profile: CompanyProfile,
        processing: NewsProcessingResult,
        context: CompanyContext,
        *,
        reason: str,
        model_id: str,
        prompt_version: str,
    ) -> CompanyNewsAssessment:
        return CompanyNewsAssessment(
            news_id=processing.news_id,
            company_id=profile.company_id,
            relevance_class=RelevanceClass.REVIEW,
            impact_direction=ImpactDirection.UNKNOWN,
            impact_type=ImpactType.UNKNOWN,
            impact_horizon=ImpactHorizon.UNKNOWN,
            confidence=None,
            reasoning_summary=reason,
            supporting_fact_ids=[],
            evidence=[],
            limitations=[
                "ответ модели отклонён автоматической проверкой",
                "неподтверждённые утверждения не сохранены; нужна ручная проверка",
            ],
            missing_data=context.gaps,
            content_version=processing.content_version,
            profile_version=profile.profile_version,
            model_id=model_id,
            prompt_version=prompt_version,
            processing_version=self.settings.processing_version,
            classification_source="llm_rejected",
            created_at=utcnow_iso(),
            updated_at=utcnow_iso(),
        )
