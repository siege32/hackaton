"""Хранилище новостей (SQLite) и приём публикации в каталог.

Каждая публикация сохраняется сразу после получения (устойчивость к сбоям).
Повторный приём той же публикации идемпотентен:

- тот же URL и тот же текст  -> ``unchanged`` (ничего не меняется, LLM не нужен);
- тот же URL, другой текст   -> ``updated``: старая версия уходит в историю,
  ``content_version`` меняется, новость снова идёт в обработку;
- другой URL, тот же текст   -> ``duplicate`` (перепечатка): сохраняется как
  источник, но в обработку не идёт;
- похожая публикация в окне 72 ч -> тот же ``cluster_id``; в обработку идёт
  только если добавляет новые факты (``novelty``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ai_engine.utils import content_version as engine_content_version

from . import dedup
from .clean import CleanArticle, content_hash, normalize_url
from .prefilter import DROP, NO_TEXT_REASON, PrefilterResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    news_id            TEXT PRIMARY KEY,
    source_id          TEXT NOT NULL,
    url                TEXT,
    url_norm           TEXT UNIQUE,
    guid               TEXT,
    title              TEXT NOT NULL,
    lead               TEXT,
    text               TEXT NOT NULL,
    summary            TEXT,
    categories         TEXT,
    published_at       TEXT,
    fetched_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    language           TEXT,
    text_quality       TEXT,
    content_hash       TEXT NOT NULL,
    content_version    TEXT NOT NULL,
    duplicate_of       TEXT,
    cluster_id         TEXT,
    cluster_similarity REAL,
    novelty            TEXT,
    prefilter          TEXT,
    prefilter_reasons  TEXT,
    topics             TEXT,
    should_process     INTEGER NOT NULL DEFAULT 0,
    queued_version     TEXT
);
CREATE INDEX IF NOT EXISTS news_hash ON news(content_hash);
CREATE INDEX IF NOT EXISTS news_cluster ON news(cluster_id);
CREATE INDEX IF NOT EXISTS news_published ON news(published_at);

CREATE TABLE IF NOT EXISTS news_history (
    news_id         TEXT NOT NULL,
    content_version TEXT NOT NULL,
    title           TEXT,
    text            TEXT,
    replaced_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_log (
    run_id      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    stats       TEXT,
    error       TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_news_db(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _drop_textless(conn)
    return conn


def _drop_textless(conn: sqlite3.Connection) -> None:
    """Снять с анализа записи без текста, сохранённые до правила NO_TEXT_REASON."""
    with conn:
        conn.execute(
            "UPDATE news SET prefilter = ?, prefilter_reasons = ?, should_process = 0 "
            "WHERE trim(coalesce(text, '')) = '' AND prefilter != ?",
            (DROP, json.dumps([NO_TEXT_REASON], ensure_ascii=False), DROP),
        )


def reprefilter(conn: sqlite3.Connection) -> dict[str, int]:
    """Пересчитать предотбор уже загруженных новостей по текущим правилам.

    Возраст считается на момент загрузки (fetched_at), а не на сегодня — иначе
    вся старая лента разом ушла бы в DROP «устарела». should_process: DROP -> 0;
    бывший DROP, ставший KEEP/REVIEW, -> 1, если это не дубль; иначе как было.
    """
    from .prefilter import prefilter
    from .sources import MANUAL_SOURCE_ID

    counts = {"checked": 0, "changed": 0}
    rows = conn.execute(
        "SELECT news_id, source_id, title, text, categories, published_at, fetched_at, text_quality, "
        "prefilter, should_process, duplicate_of FROM news"
    ).fetchall()
    with conn:
        for r in rows:
            counts["checked"] += 1
            fetched = datetime.fromisoformat(r["fetched_at"]) if r["fetched_at"] else None
            result = prefilter(r["title"] or "", r["text"] or "", categories=json.loads(r["categories"] or "[]"),
                               published_at=r["published_at"], text_quality=r["text_quality"] or "full",
                               manual=r["source_id"] == MANUAL_SOURCE_ID, now=fetched)
            if result.decision == r["prefilter"]:
                continue
            should = bool(r["should_process"])
            if result.decision == DROP:
                should = False
            elif r["prefilter"] == DROP:
                should = r["duplicate_of"] is None
            conn.execute(
                "UPDATE news SET prefilter = ?, prefilter_reasons = ?, topics = ?, should_process = ? WHERE news_id = ?",
                (result.decision, json.dumps(result.reasons, ensure_ascii=False), json.dumps(result.topics),
                 int(should), r["news_id"]),
            )
            counts["changed"] += 1
            key = f"{r['prefilter']}->{result.decision}"
            counts[key] = counts.get(key, 0) + 1
    return counts


def make_news_id(url_norm: str | None, text_hash: str) -> str:
    key = url_norm or f"text:{text_hash}"
    return "news_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class IngestResult:
    news_id: str
    status: str  # new | updated | unchanged | duplicate
    content_version: str
    prefilter: str | None = None
    cluster_id: str | None = None
    should_process: bool = False
    details: dict[str, Any] = field(default_factory=dict)


def _recent_candidates(conn: sqlite3.Connection, published_at: str | None, exclude: str) -> list[dedup.ClusterCandidate]:
    anchor = published_at or now_iso()
    try:
        moment = datetime.fromisoformat(anchor)
    except ValueError:
        moment = datetime.now(timezone.utc)
    window = timedelta(hours=dedup.CLUSTER_WINDOW_HOURS)
    rows = conn.execute(
        """
        SELECT news_id, cluster_id, published_at, title, lead FROM news
        WHERE duplicate_of IS NULL AND news_id <> ?
          AND (published_at IS NULL OR published_at BETWEEN ? AND ?)
        ORDER BY published_at DESC LIMIT 2000
        """,
        (exclude, (moment - window).isoformat(), (moment + window).isoformat()),
    ).fetchall()
    return [
        dedup.ClusterCandidate(r["news_id"], r["cluster_id"] or r["news_id"], r["published_at"],
                               dedup.signature(r["title"], r["lead"] or ""))
        for r in rows
    ]


def ingest(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    article: CleanArticle,
    prefilter_result: PrefilterResult,
    url: str | None = None,
    guid: str = "",
    summary: str = "",
    categories: list[str] | None = None,
    published_at: str | None = None,
    language: str = "ru",
) -> IngestResult:
    """Сохранить одну очищенную публикацию. Идемпотентно (см. докстринг модуля)."""
    fetched = now_iso()
    url_norm = normalize_url(url) if url else None
    text_hash = content_hash(article.title, article.text)

    existing = None
    if url_norm:
        existing = conn.execute("SELECT * FROM news WHERE url_norm = ?", (url_norm,)).fetchone()
    if existing is None:
        existing = conn.execute(
            "SELECT * FROM news WHERE news_id = ?", (make_news_id(url_norm, text_hash),)
        ).fetchone()

    if existing is not None:
        news_id = existing["news_id"]
        if existing["content_hash"] == text_hash:
            return IngestResult(news_id, "unchanged", existing["content_version"], existing["prefilter"],
                                existing["cluster_id"], bool(existing["should_process"]))
        version = engine_content_version(news_id, article.title, article.text)
        should = prefilter_result.decision != DROP and existing["duplicate_of"] is None
        with conn:
            conn.execute(
                "INSERT INTO news_history VALUES (?, ?, ?, ?, ?)",
                (news_id, existing["content_version"], existing["title"], existing["text"], fetched),
            )
            conn.execute(
                """
                UPDATE news SET title=?, lead=?, text=?, summary=?, content_hash=?, content_version=?,
                       text_quality=?, updated_at=?, prefilter=?, prefilter_reasons=?, topics=?, should_process=?
                WHERE news_id=?
                """,
                (article.title, article.lead, article.text, summary, text_hash, version, article.quality, fetched,
                 prefilter_result.decision, json.dumps(prefilter_result.reasons, ensure_ascii=False),
                 json.dumps(prefilter_result.topics), int(should), news_id),
            )
        return IngestResult(news_id, "updated", version, prefilter_result.decision, existing["cluster_id"], should)

    news_id = make_news_id(url_norm, text_hash)
    version = engine_content_version(news_id, article.title, article.text)
    duplicate = conn.execute(
        "SELECT news_id, cluster_id FROM news WHERE content_hash = ? AND duplicate_of IS NULL", (text_hash,)
    ).fetchone()

    details: dict[str, Any] = {}
    if duplicate is not None:
        status, duplicate_of, cluster_id, similarity = "duplicate", duplicate["news_id"], duplicate["cluster_id"], 1.0
        novelty_info, should = None, False
        details["duplicate_of"] = duplicate_of
    else:
        status, duplicate_of = "new", None
        cluster_id, similarity = dedup.find_cluster(
            article.title, article.lead, published_at, _recent_candidates(conn, published_at, news_id)
        )
        novelty_info = None
        if cluster_id is None:
            cluster_id = news_id
            first_in_cluster = True
        else:
            first_in_cluster = False
            known = [r["text"] for r in conn.execute(
                "SELECT text FROM news WHERE cluster_id = ? AND duplicate_of IS NULL", (cluster_id,)
            )]
            novelty_info = dedup.novelty(article.text, known)
            details["novelty"] = novelty_info.to_dict()
        should = prefilter_result.decision != DROP and (first_in_cluster or novelty_info.adds_facts)

    with conn:
        conn.execute(
            """
            INSERT INTO news (news_id, source_id, url, url_norm, guid, title, lead, text, summary, categories,
                              published_at, fetched_at, updated_at, language, text_quality, content_hash,
                              content_version, duplicate_of, cluster_id, cluster_similarity, novelty, prefilter,
                              prefilter_reasons, topics, should_process)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (news_id, source_id, url, url_norm, guid, article.title, article.lead, article.text, summary,
             json.dumps(categories or [], ensure_ascii=False), published_at, fetched, fetched, language,
             article.quality, text_hash, version, duplicate_of, cluster_id, similarity,
             json.dumps(novelty_info.to_dict(), ensure_ascii=False) if novelty_info else None,
             prefilter_result.decision, json.dumps(prefilter_result.reasons, ensure_ascii=False),
             json.dumps(prefilter_result.topics), int(should)),
        )
    return IngestResult(news_id, status, version, prefilter_result.decision, cluster_id, should, details)


def known_url(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute("SELECT 1 FROM news WHERE url_norm = ?", (normalize_url(url),)).fetchone() is not None


def log_fetch(conn: sqlite3.Connection, run_id: str, source_id: str, status: str,
              stats: dict | None = None, error: str | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO fetch_log VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, source_id, now_iso(), status, json.dumps(stats or {}, ensure_ascii=False), error),
        )
