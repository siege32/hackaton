"""Поиск по распознанной аналитике Альфа-Банка (шаг 15 ТЗ).

Фрагменты режутся по абзацам внутри одной страницы (страница = единица
цитирования), ранжирование — BM25 с упрощённым стеммингом (обрезка слова до
основы фиксированной длины). Этого достаточно для 9 отчётов и не требует
эмбеддингов, сети или платных вызовов.

Адаптер ``ai_engine.storage.base.AnalyticsIndex``: ``search(query, limit)``
возвращает ``AnalyticsSnippet`` только из реально распознанного текста.
Пустой результат — пустой список (ai_engine не должен подставлять «типовые» тексты).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ai_engine.storage.base import AnalyticsSnippet

from .catalog import ReportDocument, document_by_id

#: Отчёт старше этого числа дней относительно as_of помечается как устаревший.
STALE_AFTER_DAYS = 365
_CHUNK_TARGET = 900
#: Фрагменты короче этого склеиваются со следующим абзацем (заголовки, подписи).
_CHUNK_MIN = 250
#: Абзац короче этого прямо перед графиком/таблицей считается их подписью.
_CAPTION_MAX = 200
_STEM = 6
_MIN_SCORE = 0.5
#: Сколько лучших фрагментов упорядочивается от новых отчётов к старым (см. ``search``).
_NEWEST_FIRST = 3
_BOILERPLATE = (
    "все права защищены", "генеральная лицензия цб рф", "не является деятельностью по инвестиционному",
    "являются исключительной собственностью", "дисклеймер", "несанкционированное копирование",
    "аффилированные лица ведут и намерены",
)
_TOKEN = re.compile(r"[a-zа-я0-9]+(?:[.,][0-9]+)?")
_STOP = {
    "и", "в", "во", "на", "с", "со", "по", "к", "о", "об", "от", "до", "за", "из", "у", "не", "что", "как", "а",
    "но", "это", "для", "при", "же", "ли", "или", "то", "так", "также", "его", "ее", "их", "мы", "наш", "наша",
    "наши", "был", "была", "были", "быть", "будет", "есть", "уже", "еще", "все", "этот", "эта", "эти", "который",
    "которая", "которые", "the", "of", "and", "in", "to",
}


#: Частые окончания (длинные первыми). Грубо, но связывает «ставка/ставки/ставкой».
_ENDINGS = tuple(sorted((
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ыми", "иях", "ах", "ях", "ой", "ей", "ий", "ый",
    "ая", "яя", "ое", "ее", "ые", "ие", "ом", "ем", "ов", "ев", "ам", "ям", "ую", "юю", "а", "я", "ы", "и",
    "е", "о", "у", "ю", "ь",
), key=len, reverse=True))


def stem(word: str) -> str:
    if not word[0].isalpha():
        return word
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            word = word[: -len(ending)]
            break
    return word[:_STEM]


def tokenize(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    return [stem(t) for t in _TOKEN.findall(text) if t not in _STOP and len(t) > 1]


def with_bigrams(tokens: list[str]) -> list[str]:
    """Слова + соседние пары: «ключевой ставки» как фраза весит больше, чем два слова врозь."""
    return tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]


@dataclass
class Chunk:
    chunk_id: str
    document_id: str
    page: int
    text: str
    confidence: str | None
    tokens: list[str] = field(default_factory=list)
    #: ``text`` — текст аналитика, ``table`` — таблица, ``chart`` — описание графика.
    kind: str = "text"


#: Вид фрагмента -> множитель оценки. Описания графиков и таблицы — подписи и ряды
#: чисел: совпадение слов в подписи («Динамика курса рубля») не значит, что там есть
#: позиция банка, поэтому при равной релевантности выше текст аналитика.
KIND_WEIGHTS = {"text": 1.0, "table": 0.6, "chart": 0.5}

_CHART_MARK = re.compile(r"\[(?:График|Диаграмма|Рисунок|Схема)\]\s*", re.I)
#: Строки-артефакты распознавания: логотип «А», колонтитулы, номера страниц, источники.
_NOISE_LINE = re.compile(
    r"^(?:а|альфа[- ]?банк|центр макроэкономического анализа|рынок облигаций|бонд бриф: итоги недели|\d{1,3}"
    r"|\[(?:логотип|изображение|фото|иконка)[^\]]*\]|источник[и]?:.*|примечание:.*|аналитика альфа-киб.*|-{3,}|_{3,})$",
    re.I,
)
#: Маркеры списков из символьных шрифтов PDF («□», «❑», «», «») -> обычная точка списка.
_BULLETS = re.compile(r"^[ \t>]*[□❑■▪◆●♦➢➤►�][ \t]*", re.M)


def clean_text(text: str) -> str:
    """Убрать разметку Markdown и артефакты распознавания, оставив смысловой текст."""
    text = text.replace("\\*", "*")
    text = _BULLETS.sub("• ", text)
    text = re.sub(r"[□❑■▪◆♦�]", "", text)
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"\1", text)  # *курсив*
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^#{1,6}\s*", "", line).strip()
        if line.startswith("*") and not line.startswith("* "):
            line = line.lstrip("*").strip()  # сноска «*Источники: …»
        if _NOISE_LINE.match(line.rstrip(".*").strip()):
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _chart_summary(block: str) -> str:
    """Описание графика без шкал: название, легенда, подписанные значения.

    Шкала значений (ось Y) — ряд чисел без смысла, её убираем. Ось X (даты, периоды)
    оставляем, только если на графике есть подписанные значения: иначе прогноз
    «16,0%, 15,5% … 12,0%» потеряет даты, к которым относится.
    """
    parts = []
    for line in block.splitlines():
        # Однострочный формат «[График] Название. Ось Y: 90–120. Легенда: …» режем по предложениям.
        parts += re.split(r"(?<=\.)\s+(?=[А-ЯA-Z])", line) if "Ось" in line and not line.lstrip("- ").startswith("Ось") else [line]
    parts = [p.strip().lstrip("- ").strip() for p in parts]
    labelled = any(re.match(r"Подпис", p) for p in parts)
    kept = [
        _CHART_MARK.sub("График: ", p, count=1) for p in parts
        if p and not (re.match(r"Ось\b", p) and not (labelled and re.match(r"Ось [XХ]\b", p)))
    ]
    return "\n".join(kept)


def _is_caption(para: str) -> bool:
    """Подпись или заголовок («Рис. 5: …», «Динамика курса рубля»), а не предложение аналитика."""
    if len(para) >= _CAPTION_MAX or "\n" in para.strip():
        return False
    return bool(re.match(r"(?:Рис|Табл|Таблица|График|Диаграмма)\b", para)) or not re.search(r"[.!?…](?:\s|$)", para)


def _blocks(text: str) -> list[tuple[str, str]]:
    """Абзацы очищенной страницы с видом: ``text`` / ``table`` / ``chart``."""
    out = []
    for para in re.split(r"\n\s*\n", clean_text(text)):
        para = para.strip()
        if not para or any(marker in para.lower() for marker in _BOILERPLATE):
            continue
        lines = para.splitlines()
        if _CHART_MARK.search(para):
            head, _, chart = para.partition(_CHART_MARK.search(para).group(0))
            if head.strip() and not _CHART_MARK.sub("", chart).startswith(head.strip()[:30]):
                out.append(("text", head.strip()))  # текст перед графиком в том же абзаце
            out.append(("chart", _chart_summary(_CHART_MARK.search(para).group(0) + chart)))
        elif sum(line.lstrip().startswith("|") for line in lines) >= max(2, len(lines) // 2):
            out.append(("table", para))
        else:
            out.append(("text", para))
    # Подпись графика или таблицы («Рис. 5: …», «Табл.1: …») обычно стоит отдельным
    # коротким абзацем прямо перед ними: если она повторяет название графика — дубль,
    # иначе становится первой строкой графика/таблицы. Отдельным крошечным фрагментом
    # её оставлять нельзя: BM25 завышает оценку коротким фрагментам.
    merged: list[tuple[str, str]] = []
    for kind, para in out:
        if kind != "text" and merged and merged[-1][0] == "text" and _is_caption(merged[-1][1]):
            caption = merged.pop()[1]
            if caption.rstrip(".:") not in para:
                para = f"{caption}\n{para}"
        merged.append((kind, para))
    return merged


def _split_page(text: str) -> list[tuple[str, str]]:
    """Фрагменты страницы с видом: текст аналитика склеивается по абзацам до ~_CHUNK_TARGET
    символов, графики и таблицы идут отдельными фрагментами (у них ниже вес в поиске)."""
    chunks: list[tuple[str, str]] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if not current:
            return
        if chunks and chunks[-1][0] == "text" and len(current) < _CHUNK_MIN:
            chunks[-1] = ("text", f"{chunks[-1][1]}\n\n{current}")
        else:
            chunks.append(("text", current))
        current = ""

    for kind, para in _blocks(text):
        if kind != "text":
            if current and _is_caption(current) and (not chunks or chunks[-1][0] != "text"):
                # Короткий текст между графиками (заголовок раздела) — подпись к следующему.
                para, current = f"{current}\n{para}", ""
            flush()
            chunks.append((kind, para))
        elif current and len(current) >= _CHUNK_MIN and len(current) + len(para) > _CHUNK_TARGET:
            flush()
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    flush()
    return chunks


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


class BankReportsIndex:
    """BM25-индекс по JSON-страницам из ``data/bank_reports/ocr``."""

    def __init__(self, ocr_dir: str | Path, *, as_of: date | None = None, k1: float = 1.5, b: float = 0.75) -> None:
        self.ocr_dir = Path(ocr_dir)
        self.as_of = as_of or date.today()
        self.k1, self.b = k1, b
        self.chunks: list[Chunk] = []
        self._load()
        self._df: Counter[str] = Counter()
        for chunk in self.chunks:
            self._df.update(set(chunk.tokens))
        self._avg_len = (sum(len(c.tokens) for c in self.chunks) / len(self.chunks)) if self.chunks else 0.0

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.ocr_dir.exists():
            return
        for path in sorted(self.ocr_dir.glob("*/page_*.json")):
            page = json.loads(path.read_text(encoding="utf-8"))
            doc = document_by_id(page["document_id"])
            title_tokens = tokenize(" ".join([doc.title, *doc.topics])) if doc else []
            parts = [(k, t) for k, t in _split_page(page.get("text") or "")
                     if len(re.findall(r"[а-яёa-z]", t, re.I)) >= 40]
            for n, (kind, text) in enumerate(parts, start=1):
                self.chunks.append(Chunk(
                    chunk_id=f"{page['document_id']}:p{page['page']}:c{n}",
                    document_id=page["document_id"],
                    page=page["page"],
                    text=text,
                    confidence=page.get("confidence"),
                    tokens=with_bigrams(tokenize(text)) + title_tokens,
                    kind=kind,
                ))

    @property
    def documents(self) -> list[str]:
        return sorted({c.document_id for c in self.chunks})

    def _score(self, query: list[str], chunk: Chunk) -> float:
        tf = Counter(chunk.tokens)
        n = len(self.chunks)
        score = 0.0
        for term in set(query):
            if term not in tf:
                continue
            idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
            norm = tf[term] + self.k1 * (1 - self.b + self.b * len(chunk.tokens) / self._avg_len)
            score += idf * tf[term] * (self.k1 + 1) / norm
        return score

    def is_stale(self, doc: ReportDocument | None) -> bool | None:
        report_date = _parse_date(doc.report_date) if doc else None
        if report_date is None:
            return None
        return (self.as_of - report_date).days > STALE_AFTER_DAYS

    def _snippet(self, chunk: Chunk, score: float) -> AnalyticsSnippet:
        doc = document_by_id(chunk.document_id)
        title = doc.title if doc else chunk.document_id
        when = doc.report_date or doc.period if doc else "дата неизвестна"
        return AnalyticsSnippet(
            snippet_id=chunk.chunk_id,
            title=title,
            text=chunk.text,
            source=f"Альфа-Банк, «{title}», {when}, стр. {chunk.page}",
            score=round(score, 2),
            url=None,
            metadata={
                "document_id": chunk.document_id,
                "file": doc.file if doc else None,
                "page": chunk.page,
                "report_date": doc.report_date if doc else None,
                "period": doc.period if doc else None,
                "series": doc.series if doc else None,
                "stale": self.is_stale(doc),
                "notes": list(doc.notes) if doc else [],
                "ocr_confidence": chunk.confidence,
                "kind": chunk.kind,
                "origin": "bank_analytics",
            },
        )

    def freshness(self, document_id: str) -> float:
        """1 для отчёта на дату as_of, 0,5 — годичной давности; без даты — как полугодовой."""
        doc = document_by_id(document_id)
        report_date = _parse_date(doc.report_date) if doc else None
        age_days = max(0, (self.as_of - report_date).days) if report_date else 182
        return 1.0 / (1.0 + age_days / 365)

    def search(self, query: str, limit: int = 3) -> list[AnalyticsSnippet]:
        """Протокол ``AnalyticsIndex``: лучшие фрагменты, не больше одного на страницу.

        1. Отбор по релевантности BM25 с мягкой поправкой на свежесть (не больше 25 %),
           чтобы старый, но точный по теме отчёт не вытеснялся свежим мусором,
           и на вид фрагмента (``KIND_WEIGHTS``: графики и таблицы ниже текста).
        2. Три лучших фрагмента упорядочены от новых отчётов к старым: банк меняет позицию
           (прогноз ставки на конец 2026 г.: 13,5 % в отчёте от 31.07 -> 14,0 % от 11.09),
           и агент должен сначала видеть самую свежую. Остальные — по оценке.
        """
        terms = with_bigrams(tokenize(query))
        if not terms or not self.chunks:
            return []
        scored = []
        for chunk in self.chunks:
            relevance = self._score(terms, chunk)
            if relevance >= _MIN_SCORE:
                weight = (0.75 + 0.25 * self.freshness(chunk.document_id)) * KIND_WEIGHTS[chunk.kind]
                scored.append((relevance * weight, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        picked, seen_pages = [], set()
        for score, chunk in scored:
            if len(picked) >= limit:
                break
            key = (chunk.document_id, chunk.page)
            if key in seen_pages:
                continue
            seen_pages.add(key)
            picked.append((score, chunk))

        def newest_first(item: tuple[float, Chunk]) -> tuple[str, float]:
            doc = document_by_id(item[1].document_id)
            return (doc.report_date or "" if doc else "", item[0])

        # От новых к старым — только лучшие _NEWEST_FIRST (столько берёт анализ); дальше по
        # оценке. Иначе в выдаче интерфейса (8 фрагментов) слабый, но свежий фрагмент
        # («Полюс» по запросу «курс рубля», 2,86) вставал над точными (7–11).
        head = sorted(picked[:_NEWEST_FIRST], key=newest_first, reverse=True)
        picked = head + picked[_NEWEST_FIRST:]
        return [self._snippet(chunk, score) for score, chunk in picked]


def search_bank_reports(
    index: BankReportsIndex,
    topic: str,
    company_context: str | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    """Инструмент агента ``search_bank_reports`` (шаг 14 ТЗ) со статусом.

    ``company_context`` (например, отрасль) добавляется к запросу как
    дополнительные слова: фрагменты, где совпадают и тема, и контекст, выше.
    """
    if not index.chunks:
        return {"tool": "search_bank_reports", "status": "error", "data": [],
                "message": f"Индекс аналитики пуст: нет распознанных отчётов в {index.ocr_dir}"}
    query = f"{topic} {company_context}" if company_context else topic
    snippets = index.search(query, limit=limit)
    if not snippets:
        return {"tool": "search_bank_reports", "status": "empty", "data": [], "query": query,
                "message": "В аналитике Альфа-Банка не найдено материалов по теме. "
                           "Нельзя выдавать общий вывод модели за позицию банка."}
    warnings = []
    dates = {s.metadata.get("report_date") for s in snippets if s.metadata.get("report_date")}
    if len(dates) > 1:
        warnings.append(
            f"Фрагменты из отчётов разных дат ({', '.join(sorted(dates, reverse=True))}): позиция банка могла "
            "измениться — опираться на самый свежий отчёт (выдача отсортирована от новых к старым)."
        )
    if any(s.metadata.get("stale") for s in snippets):
        warnings.append("Часть материалов устарела (старше года): использовать только как исторический контекст.")
    if any(s.metadata.get("ocr_confidence") in ("low", "unverified") for s in snippets):
        warnings.append("Часть текста распознана с низкой уверенностью: цитаты сверять со страницей PDF.")
    return {"tool": "search_bank_reports", "status": "success", "query": query,
            "data": [s.to_dict() for s in snippets], "warnings": warnings}
