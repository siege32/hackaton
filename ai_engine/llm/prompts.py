"""Versioned prompt templates.

Prompt versions are stored with every result so that a change in instructions
does not silently mix incompatible results.  Bump the version whenever the
expected semantics change.
"""

from __future__ import annotations

import json
from typing import Any

from ..models import CompanyContext, CompanyNewsAssessment, InsightResult, NewsProcessingResult, Refinement
from ..utils import jsonable, truncate_text

PROMPT_VERSIONS = {
    "facts": "news-facts.v2",
    "company_assessment": "company-relevance.v2",
    "insight": "company-insight.v1",
    "adaptation": "company-adaptation.v3",
}

SYSTEM_BASE = """Ты — аналитический компонент системы мониторинга новостей SXSVN.
Твои ответы используются программным кодом, поэтому:
- отвечай ТОЛЬКО одним JSON-объектом без Markdown, пояснений и комментариев;
- не выдумывай факты, компании, числа, даты, связи или источники;
- если данных нет, используй пустой список/строку/значение "unknown";
- цитаты (quote) должны быть дословными фрагментами предоставленного текста;
- различай утверждение источника и собственную гипотезу;
- не превращай отсутствие данных в отрицательный факт.
Текстовые поля, которые читает пользователь (summary, mechanism, reason, limitations,
recommended_checks, risks, opportunities и т.п.), пиши по-русски обычным языком:
- без названий полей и служебных значений из входных данных (не «confirmed=false»,
  а «не подтверждено»; не «impact_direction=unknown», а «направление не определено»;
  не «user_statement», а «заявление пользователя»);
- без технических идентификаторов (group_…, news_…, U_…, analysis_…) и имён файлов
  (.csv): пиши «группа компаний», «эта новость», «компания», «данные банка о платежах».
Значения enum-полей (relevance_class, impact_direction и т.п.) и ref_id оставляй как в схеме.
"""


def _json(data: Any) -> str:
    return json.dumps(jsonable(data), ensure_ascii=False, indent=2)


def facts_prompts(news_fields: dict[str, Any], max_chars: int) -> tuple[str, str]:
    """Prompts for stage A: company-independent facts extraction."""
    text, truncated = truncate_text(str(news_fields.get("text") or ""), max_chars)
    payload = {
        "news_id": news_fields.get("news_id"),
        "title": news_fields.get("title"),
        "source_id": news_fields.get("source_id"),
        "published_at": news_fields.get("published_at"),
        "url": news_fields.get("url"),
        "text": text,
        "text_truncated": truncated,
    }
    system = SYSTEM_BASE + """
Задача: извлечь из новости общие факты, не зависящие от какой-либо конкретной компании.
Верни JSON строго такой структуры:
{
  "event_type": "краткий тип события, например price_change / regulation / contract / production / sanctions / other",
  "main_events": ["1-5 кратких описаний того, что сообщает источник"],
  "general_relevance": "KEEP | DROP | REVIEW",
  "classification_reason": "почему новость вообще стоит или не стоит анализировать",
  "entities": {
    "companies": [], "organizations": [], "people": [], "products": [],
    "resources": [], "dates": [], "geographies": [], "event_types": []
  },
  "facts": [
    {
      "fact_type": "price_change | supply | demand | regulation | contract | production | finance | sanctions | logistics | other",
      "subject": "кто/что",
      "action": "что произошло (глагол/изменение)",
      "object": "объект действия или null",
      "date": "дата/период как в тексте или null",
      "period": "период или null",
      "quantities": [{"value": 12.5, "unit": "%", "raw": "12,5%"}],
      "conditions": ["условия, при которых утверждение верно"],
      "negated": false,
      "origin": "source | model_inference",
      "certainty": "asserted | uncertain",
      "quote": "дословный фрагмент текста, подтверждающий факт",
      "related_fact_ids": [],
      "extra": {}
    }
  ],
  "uncertainties": ["что в тексте неясно"],
  "contradictions": ["противоречия внутри источника"],
  "missing_information": ["каких существенных сведений нет"]
}
Правила:
- general_relevance — бизнес-класс, а не статус обработки. REVIEW допустим при неопределённости.
- DROP допустим только если новость заведомо не относится к экономическим/деловым событиям (например, светская хроника); сомнительные новости — REVIEW.
- quantities[].value — число, если его можно однозначно прочитать из raw; raw сохраняй как в тексте. Ничего не пересчитывай.
- Не добавляй факты, которых нет в тексте. Если фактов нет, верни пустой список facts.
"""
    return system + "\nLimit the output to at most 12 material facts. Keep each fact concise and each quote to one short verbatim sentence. Return no more than 3 main_events.", "Данные новости:\n" + _json(payload)


def company_assessment_prompts(context: CompanyContext, news: NewsProcessingResult) -> tuple[str, str]:
    """Prompts for stage B: relevance for one concrete company."""
    facts = [f.to_dict() for f in context.news_facts]
    profile = context.profile.to_dict()
    relationships = [r.to_dict() for r in context.relationships]
    exposures = [e.to_dict() for e in context.exposures]
    payload = {
        "news_id": news.news_id,
        "news_content_version": news.content_version,
        "event_type": news.event_type,
        "main_events": news.main_events,
        "entities": news.entities.to_dict(),
        "facts": facts,
        "uncertainties": news.uncertainties,
        "contradictions": news.contradictions,
        "company_id": context.profile.company_id,
        "company_profile": profile,
        "profile_version": context.profile.profile_version,
        "confirmed_relationships": relationships,
        "candidate_exposures": exposures,
        "profile_gaps": context.gaps,
    }
    system = SYSTEM_BASE + """
Задача: оценить, имеет ли данная новость обоснованную потенциально значимую связь
именно с данной компанией, и определить направление/тип возможного влияния.

Верни JSON строго такой структуры:
{
  "news_id": "как во входе",
  "company_id": "как во входе",
  "relevance_class": "KEEP | DROP | REVIEW",
  "impact_direction": "positive | negative | mixed | unknown",
  "impact_type": "direct | indirect | unknown",
  "impact_mechanism": "конкретный механизм связи события с деятельностью компании",
  "impact_horizon": "short_term | medium_term | long_term | unknown",
  "confidence": 0.0,
  "reasoning_summary": "краткое обоснование решения",
  "supporting_fact_ids": ["fact_id из входных facts"],
  "evidence": [{"fact_id": "...", "source_id": "...", "quote": "дословная цитата", "supports": "что подтверждает"}],
  "limitations": ["ограничения вывода"],
  "missing_data": ["каких данных о компании не хватает"]
}
Правила классификации:
- KEEP: есть обоснованный механизм связи с компанией (прямой или через подтверждённую зависимость/рынок).
- DROP: после проверки доступных данных значимая связь не обнаружена.
- REVIEW: связь неоднозначна, профиль неполон, данных недостаточно или источники противоречат друг другу.
- Если для вывода о связи не хватает сведений об операциях, маршрутах, объектах или контрагентах компании, выбирай REVIEW, а не DROP; перечисли конкретные пробелы в missing_data.
- Отсутствие названия компании в тексте НЕ является основанием для DROP.
- Совпадение отрасли само по себе НЕ является основанием для KEEP.
- Неизвестная зависимость не является подтверждённым фактом. Платёжная ссылка/отдельный платёж не доказывают зависимость.
- Не считай отсутствующие в профиле сведения отрицательными фактами.
- impact_direction и impact_type — отдельные поля; если направление нельзя обосновать, используй unknown.
- confidence — предварительная уверенность в выводе (0..1), НЕ вероятность финансового убытка. Можно null.
- evidence должен ссылаться только на существующие fact_id; quote — дословный фрагмент текста новости.
- Не придумывай финансовые последствия и числовые оценки, если их нет в данных.
"""
    return system, "Входные данные:\n" + _json(payload)


def insight_prompts(
    company: dict[str, Any],
    events: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    analytics: list[dict[str, Any]],
    calculations: list[dict[str, Any]],
    additional_searches_left: int,
) -> tuple[str, str]:
    payload = {
        "company": company,
        "events": events,
        "relationships": relationships,
        "bank_analytics_snippets": analytics,
        "available_calculations": calculations,
        "additional_searches_left": additional_searches_left,
    }
    system = SYSTEM_BASE + """
Задача: построить доказательную цепочку возможного влияния уже отобранных событий
на конкретную компанию. Не пересказывай новости, а объясняй связь.

Верни JSON строго такой структуры:
{
  "summary": "краткий итог",
  "events": [
    {
      "news_id": "как во входе",
      "relevance_class": "KEEP | REVIEW",
      "impact_direction": "positive | negative | mixed | unknown",
      "impact_type": "direct | indirect | unknown",
      "mechanism": "механизм влияния",
      "potential_consequences": ["возможные последствия, не выдавай их за наступивший ущерб"],
      "evidence": [{"kind": "fact | relationship | analytics | calculation | inference | hypothesis",
                    "ref_id": "id из входных данных", "quote": "...", "source_id": "...",
                    "supports": "...", "confirmed": true}],
      "limitations": ["чего мы не знаем"],
      "confidence": null,
      "impact_horizon": "short_term | medium_term | long_term | unknown",
      "chain": ["факт события", "связь с компанией", "механизм", "возможное последствие"],
      "hypothesis": false
    }
  ],
  "key_risks": [], "potential_opportunities": [], "unknowns": [],
  "recommended_checks": [], "limitations": []
}
Правила:
- evidence.kind=fact/relationship/analytics/calculation допускается только при наличии соответствующего id во входных данных.
- Не ссылайся на банковскую аналитику, которой нет в bank_analytics_snippets.
- Фрагменты bank_analytics_snippets подобраны поиском по ключевым словам и могут быть не по теме.
  Добавляй в evidence (kind=analytics) только те, что прямо говорят о теме события или о
  факторе, через который оно влияет на компанию (ставка, курс, спрос отрасли и т.п.).
  Нерелевантные фрагменты не цитируй; если подходящих нет — не добавляй analytics вовсе
  и напиши в limitations, что в аналитике банка позиции по этой теме нет.
- Если по входным данным связь события с деятельностью компании не просматривается,
  не выдумывай канал влияния: impact_direction="unknown", низкая confidence, и прямо
  напиши в summary, что новость не затрагивает компанию по имеющимся данным.
- Не придумывай доли поставок, суммы, вероятности и финансовый эффект.
- Гипотезы помечай hypothesis=true и kind=hypothesis/inference.
- potential_consequences — возможности, а не уже наступившие последствия.
- Учитывай только подтверждённые связи; неподтверждённые — в unknowns/limitations.
"""
    return system, "Входные данные:\n" + _json(payload)


def adaptation_prompts(
    analysis: InsightResult,
    refinement: Refinement,
    context: CompanyContext,
) -> tuple[str, str]:
    payload = {
        "previous_analysis": analysis.to_dict(),
        "user_refinement": refinement.to_dict(),
        "affected_event_ids": refinement.affected_event_ids,
        "refinement_status": "это утверждение пользователя, НЕ подтверждённый внешний факт",
        "company_profile": context.profile.to_dict(),
        "confirmed_relationships": [r.to_dict() for r in context.relationships],
        "candidate_exposures": [e.to_dict() for e in context.exposures],
        "news_facts": [fact.to_dict() for fact in context.news_facts],
        "profile_gaps": context.gaps,
    }
    system = SYSTEM_BASE + """
Задача: пересмотреть предыдущий анализ с учётом уточнения пользователя.
Верни JSON той же структуры, что и предыдущий анализ:
{"summary": "...", "events": [...], "key_risks": [], "potential_opportunities": [], "unknowns": [], "recommended_checks": [], "limitations": []}
Допустимые значения: relevance_class=KEEP|REVIEW; impact_direction=positive|negative|mixed|unknown; impact_type=direct|indirect|unknown; impact_horizon=short_term|medium_term|long_term|unknown. Если уточнение снимает механизм влияния или направление нельзя подтвердить, укажи unknown (не используй neutral).
Правила:
- Пересматривай только выводы, которые зависят от уточнения. Остальные события сохраняй.
- В поле events верни только затронутые события из affected_event_ids.
- В key_risks и potential_opportunities сохрани подробные прежние пункты, не связанные с уточнением; обновляй только зависимые от него пункты. Не заменяй пояснения короткими метками с news_id.
- Уточнение пользователя маркируй как user_statement; не выдавай его за подтверждённый внешний факт.
- Не пиши в limitations, что уточнение не подтверждено или исходит от пользователя — система
  добавит это одной строкой сама. Не повторяй одну и ту же мысль в разных пунктах limitations.
- Исходный текст новости не переписывай.
- Не добавляй новые факты, числа или источники.
- Используй DROP только если kind=user_refinement.not_relevant; прочие уточнения должны пересмотреть зависимые выводы без автоматического исключения события.
- Изменённые события должны сохранять доступные evidence.
"""
    return system, "Входные данные:\n" + _json(payload)
