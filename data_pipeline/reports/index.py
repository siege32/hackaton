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
_STEM = 6
_MIN_SCORE = 0.5
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


def _split_page(text: str) -> list[str]:
    """Абзацы страницы, склеенные до ~_CHUNK_TARGET символов; служебный текст отбрасывается."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        if any(marker in para.lower() for marker in _BOILERPLATE):
            continue
        if current and len(current) >= _CHUNK_MIN and len(current) + len(para) > _CHUNK_TARGET:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        if chunks and len(current) < _CHUNK_MIN:
            chunks[-1] = f"{chunks[-1]}\n\n{current}"
        else:
            chunks.append(current)
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
            texts = [t for t in _split_page(page.get("text") or "") if len(re.findall(r"[а-яёa-z]", t, re.I)) >= 40]
            for n, text in enumerate(texts, start=1):
                self.chunks.append(Chunk(
                    chunk_id=f"{page['document_id']}:p{page['page']}:c{n}",
                    document_id=page["document_id"],
                    page=page["page"],
                    text=text,
                    confidence=page.get("confidence"),
                    tokens=with_bigrams(tokenize(text)) + title_tokens,
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
           чтобы старый, но точный по теме отчёт не вытеснялся свежим мусором.
        2. Выдача упорядочена от новых отчётов к старым: банк меняет позицию
           (прогноз ставки на конец 2026 г.: 13,5 % в отчёте от 31.07 -> 14,0 % от 11.09),
           и агент должен сначала видеть самую свежую.
        """
        terms = with_bigrams(tokenize(query))
        if not terms or not self.chunks:
            return []
        scored = []
        for chunk in self.chunks:
            relevance = self._score(terms, chunk)
            if relevance >= _MIN_SCORE:
                weight = 0.75 + 0.25 * self.freshness(chunk.document_id)
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

        picked.sort(key=newest_first, reverse=True)
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
