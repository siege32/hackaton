"""Программный предотбор новостей (шаг 5 ТЗ) до любых вызовов LLM.

Решение общее для всех компаний: есть ли у публикации деловая/экономическая
тематика вообще. Соответствие конкретной компании здесь НЕ проверяется —
влияние бывает косвенным (через контрагентов), это задача следующих этапов.

Словарь тем — тот же, что у классификатора ai_engine
(``ai_engine.classification.context.TOPIC_TERMS``), чтобы этапы одинаково
понимали «экономическую тему».

- KEEP   — есть экономические темы или критичные маркеры (санкции, ставка ЦБ...);
- REVIEW — тем нет, но есть признаки бизнеса (компании, рынок, отрасль) или текст неполный;
- DROP   — нет признаков деловой тематики, рубрика нерелевантна, новость устарела
  или у неё нет текста (только заголовок).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ai_engine.classification.context import TOPIC_TERMS

KEEP, DROP, REVIEW = "KEEP", "DROP", "REVIEW"

#: Новости старше этого срока на момент загрузки не анализируются (кроме ручных).
MAX_AGE_DAYS = 14
#: Сколько упоминаний тем словаря в тексте нужно для KEEP, если темы нет в заголовке.
MIN_TOPIC_HITS = 3
#: Причина DROP для новости без текста (только заголовок).
NO_TEXT_REASON = "нет текста — только заголовок, анализировать нечего"

#: Всегда в анализ: события, потенциально критичные для крупного бизнеса.
#: ``\b`` в начале обязателен: иначе «налог» находится в «аналогичный».
_CRITICAL = re.compile(
    r"\b(?:ключев\w* ставк|санкц|пошлин|эмбарго|национализ|банкротств|дефолт|отзыв\w* лицензи|"
    r"отозвал\w* лицензи|девальвац|валютн\w* контрол|экспортн\w* запрет|запрет\w* (?:на )?экспорт|налог)",
    re.I,
)
_WORD = re.compile(r"[a-zа-яё0-9-]+", re.I)
#: Слова, которые начинаются как основа темы, но к ней не относятся.
_FALSE_PREFIXES = ("газет", "газон", "портрет", "портал", "портфел", "портн", "курсант", "ставрополь", "ценност")


def topic_hits(text: str) -> dict[str, int]:
    """Упоминания тем словаря ai_engine, но только с начала слова.

    Классификатор ai_engine ищет основы как подстроки («курс» в «ресурс»,
    «ставк» в «поставка»); для отсева это даёт слишком много ложных тем.
    """
    words = [w.lower().replace("ё", "е") for w in _WORD.findall(text)]
    words = [w for w in words if not w.startswith(_FALSE_PREFIXES)]
    hits: dict[str, int] = {}
    for topic, terms in TOPIC_TERMS.items():
        stems = [t.strip().lower() for t in terms if t.strip() and " " not in t.strip()]
        count = sum(1 for w in words for s in stems if w.startswith(s))
        if count:
            hits[topic] = count
    return hits
#: Признаки деловой тематики без явной темы словаря.
_BUSINESS = re.compile(
    r"\b(?:ООО|ПАО|АО|ОАО|ЗАО|госкорпорац\w*|холдинг\w*|компани\w*|предприяти\w*|завод\w*|рын\w*к\w*|"
    r"отрасл\w*|выручк\w*|прибыл\w*|инвест\w*|сделк\w*|акци\w*|облигаци\w*|бизнес\w*|"
    # инфраструктура: удар или сбой — косвенный риск логистики и поставок для клиентов
    r"сухогруз\w*|танкер\w*|контейнер\w*|судн\w*|суд(?:а|ов|ам)|порт(?:а|у|ом|ы|ов)?|аэропорт\w*|"
    r"НПЗ|нефтебаз\w*|нефтепровод\w*|газопровод\w*|ЛЭП|электростанц\w*|подстанц\w*|железн\w* дорог\w*)\b",
    re.I,
)
_IRRELEVANT_RUBRICS = re.compile(
    r"спорт|культур|кино|шоу|звезд|стиль|путешеств|здоровье|наука и техника|интернет и сми|из жизни|"
    r"забота о себе|ценности|моя страна|игры",
    re.I,
)


@dataclass
class PrefilterResult:
    decision: str
    reasons: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    critical: bool = False


def _age_days(published_at: str | None, now: datetime) -> float | None:
    if not published_at:
        return None
    try:
        moment = datetime.fromisoformat(published_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment) / timedelta(days=1)


def prefilter(
    title: str,
    text: str,
    *,
    categories: list[str] | None = None,
    published_at: str | None = None,
    text_quality: str = "full",
    manual: bool = False,
    now: datetime | None = None,
) -> PrefilterResult:
    now = now or datetime.now(timezone.utc)
    full = f"{title}\n{text}"
    hits = topic_hits(full)
    title_topics = set(topic_hits(title))
    topics = sorted(hits)
    critical_match = _CRITICAL.search(full)
    critical = bool(critical_match)

    age = _age_days(published_at, now)
    if age is not None and age > MAX_AGE_DAYS and not manual:
        return PrefilterResult(DROP, [f"устарела: {age:.0f} дн. (> {MAX_AGE_DAYS})"], topics, critical)
    if not text.strip():
        # Источник отдал только заголовок (страница закрыта, в RSS нет анонса):
        # анализ на пустом тексте падает с failed, в ленту такое не пускаем.
        return PrefilterResult(DROP, [NO_TEXT_REASON], topics, critical)

    reasons: list[str] = []
    if critical:
        reasons.append("критичный маркер: " + critical_match.group(0).lower())
    if title_topics:
        reasons.append("темы в заголовке: " + ", ".join(sorted(title_topics)))
    strong = sum(hits.values()) >= MIN_TOPIC_HITS
    if hits:
        reasons.append("темы в тексте: " + ", ".join(f"{t}×{n}" for t, n in sorted(hits.items())))

    if critical or title_topics or strong:
        decision = KEEP
        if text_quality == "summary_only":
            decision = REVIEW
            reasons.append("полного текста нет — только анонс из RSS")
        return PrefilterResult(decision, reasons, topics, critical)

    rubric = ", ".join(categories or [])
    if rubric and _IRRELEVANT_RUBRICS.search(rubric):
        return PrefilterResult(DROP, [f"нерелевантная рубрика: {rubric}"], topics, critical)
    if hits or _BUSINESS.search(full) or manual:
        why = ("добавлена вручную" if manual else
               "слабые экономические признаки" if hits else "есть признаки бизнеса без явной экономической темы")
        return PrefilterResult(REVIEW, [why, *reasons], topics, critical)
    return PrefilterResult(DROP, ["нет признаков деловой или экономической тематики"], topics, critical)
