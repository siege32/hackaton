"""Доступ к каталогу новостей для ai_engine, API и агента.

- ``SqliteNewsRepository`` — протокол ``ai_engine.storage.base.NewsRepository``;
- ``search_news`` — инструмент агента (шаг 14 ТЗ) со статусом;
- ``list_news`` / ``cluster_sources`` — для ленты в интерфейсе и показа первоисточников.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from typing import Any

from ai_engine.models import NewsRecord

from ..reports.index import tokenize, with_bigrams
from .sources import source_by_id

_COLUMNS = ("news_id, source_id, url, title, lead, text, published_at, fetched_at, text_quality, content_version, "
            "duplicate_of, cluster_id, novelty, prefilter, prefilter_reasons, topics, categories")


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("novelty", "prefilter_reasons", "topics", "categories"):
        if key in data and data[key]:
            data[key] = json.loads(data[key])
    source = source_by_id(data.get("source_id", ""))
    data["source_name"] = source.name if source else data.get("source_id")
    return data


class SqliteNewsRepository:
    """Адаптер ``NewsRepository``: текущая версия публикации в формате ``NewsRecord``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_news(self, news_id: str) -> NewsRecord | None:
        row = self.conn.execute(f"SELECT {_COLUMNS} FROM news WHERE news_id = ?", (news_id,)).fetchone()
        if row is None:
            return None
        data = _row_dict(row)
        return NewsRecord(
            news_id=data["news_id"],
            title=data["title"],
            text=data["text"],
            source_id=data["source_id"],
            content_version=data["content_version"],
            published_at=data["published_at"],
            url=data["url"],
            language="ru",
            metadata={
                "source_name": data["source_name"],
                "lead": data["lead"],
                "fetched_at": data["fetched_at"],
                "text_quality": data["text_quality"],
                "cluster_id": data["cluster_id"],
                "duplicate_of": data["duplicate_of"],
                "novelty": data["novelty"],
                "prefilter": data["prefilter"],
                "prefilter_reasons": data["prefilter_reasons"],
                "topics": data["topics"],
                "categories": data["categories"],
            },
        )


def list_news(
    conn: sqlite3.Connection,
    *,
    prefilter: tuple[str, ...] = ("KEEP", "REVIEW"),
    since: str | None = None,
    until: str | None = None,
    source_id: str | None = None,
    include_duplicates: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Лента: по одной строке на событие (кластер) — самая ранняя публикация + число источников."""
    sql = [f"SELECT {_COLUMNS}, (SELECT COUNT(*) FROM news n2 WHERE n2.cluster_id = news.cluster_id) AS sources_in_cluster",
           "FROM news WHERE 1=1"]
    params: list[Any] = []
    if prefilter:
        sql.append(f"AND prefilter IN ({', '.join('?' for _ in prefilter)})")
        params += list(prefilter)
    if not include_duplicates:
        sql.append("AND duplicate_of IS NULL AND (cluster_id = news_id OR cluster_id IS NULL)")
    if since:
        sql.append("AND published_at >= ?")
        params.append(since)
    if until:
        sql.append("AND published_at <= ?")
        params.append(until)
    if source_id:
        sql.append("AND source_id = ?")
        params.append(source_id)
    sql.append("ORDER BY published_at DESC LIMIT ? OFFSET ?")
    params += [limit, offset]
    return [_row_dict(r) for r in conn.execute(" ".join(sql), params)]


def cluster_sources(conn: sqlite3.Connection, cluster_id: str) -> list[dict[str, Any]]:
    """Все публикации одного события по времени — с тем, что каждая добавила."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM news WHERE cluster_id = ? ORDER BY published_at", (cluster_id,)
    ).fetchall()
    return [_row_dict(r) for r in rows]


def search_news(
    conn: sqlite3.Connection,
    query: str,
    period: tuple[str | None, str | None] | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Инструмент агента ``search_news``: BM25 по заголовку и тексту, одна публикация на событие.

    ``period`` — (с, по) в ISO; ``filters`` — ``source_id``, ``prefilter`` (список), ``include_duplicates``.
    """
    filters = dict(filters or {})
    since, until = period or (None, None)
    try:
        rows = list_news(
            conn, prefilter=tuple(filters.get("prefilter", ("KEEP", "REVIEW"))), since=since, until=until,
            source_id=filters.get("source_id"), include_duplicates=bool(filters.get("include_duplicates")),
            limit=5000,
        )
    except sqlite3.Error as exc:
        return {"tool": "search_news", "status": "error", "data": [], "message": f"Каталог новостей недоступен: {exc}"}
    terms = with_bigrams(tokenize(query))
    if not rows or not terms:
        return {"tool": "search_news", "status": "empty", "data": [], "query": query,
                "message": "Новостей по условиям нет" if not rows else "Пустой запрос"}

    docs = [with_bigrams(tokenize(f"{r['title']} {r['title']} {r['text']}")) for r in rows]
    df = Counter(t for d in docs for t in set(d))
    avg = sum(len(d) for d in docs) / len(docs)
    scored = []
    for row, doc in zip(rows, docs):
        tf = Counter(doc)
        score = 0.0
        for term in set(terms):
            if term in tf:
                idf = math.log(1 + (len(docs) - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf[term] * 2.5 / (tf[term] + 1.5 * (0.25 + 0.75 * len(doc) / avg))
        if score >= 1.0:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return {"tool": "search_news", "status": "empty", "data": [], "query": query,
                "message": "По запросу ничего не найдено в каталоге новостей"}
    data = [
        {
            "news_id": r["news_id"],
            "title": r["title"],
            "lead": r["lead"],
            "url": r["url"],
            "source_id": r["source_id"],
            "source_name": r["source_name"],
            "published_at": r["published_at"],
            "cluster_id": r["cluster_id"],
            "sources_in_cluster": r["sources_in_cluster"],
            "prefilter": r["prefilter"],
            "text_quality": r["text_quality"],
            "score": round(score, 3),
        }
        for score, r in scored[:limit]
    ]
    return {"tool": "search_news", "status": "success", "query": query, "data": data}
