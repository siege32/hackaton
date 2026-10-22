"""Конвейер новостей: источники -> очистка -> дубли/кластеры -> предотбор -> очередь.

Результат — каталог событий с первоисточниками, а не готовые инсайты.
Извлечение фактов и релевантность для компании делает ai_engine по задачам
из очереди (``news.process``), один раз на версию публикации.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .clean import clean_article
from .fetch import Downloader, FeedItem, FetchError, extract_article, fetch_article, http_get, parse_rss
from .prefilter import prefilter
from .sources import MANUAL_SOURCE_ID, NewsSource, enabled_sources
from .store import IngestResult, ingest, known_url, log_fetch, now_iso

logger = logging.getLogger("data_pipeline.news.pipeline")


def _ingest_item(conn: sqlite3.Connection, source: NewsSource, item: FeedItem, page_text: str,
                 page_date: str | None, *, manual: bool = False) -> IngestResult:
    text = item.full_text or page_text
    article = clean_article(item.title, text, item.summary)
    published = item.published_at or page_date
    decision = prefilter(article.title, article.text, categories=item.categories, published_at=published,
                         text_quality=article.quality, manual=manual)
    return ingest(conn, source_id=source.source_id, article=article, prefilter_result=decision, url=item.url or None,
                  guid=item.guid, summary=item.summary, categories=item.categories, published_at=published)


def fetch_source(
    conn: sqlite3.Connection,
    source: NewsSource,
    *,
    download: Downloader = http_get,
    max_items: int | None = None,
    workers: int = 6,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Загрузить один источник. Ошибка источника не роняет весь прогон и отличается от «пусто»."""
    run_id = run_id or uuid.uuid4().hex[:12]
    stats: dict[str, Any] = {"source_id": source.source_id, "items": 0, "new": 0, "updated": 0, "unchanged": 0,
                             "duplicate": 0, "page_errors": 0, "KEEP": 0, "REVIEW": 0, "DROP": 0, "to_process": 0}
    try:
        items = parse_rss(download(source.rss_url))
    except FetchError as exc:
        log_fetch(conn, run_id, source.source_id, "error", stats, str(exc))
        return {**stats, "status": "error", "error": str(exc)}
    items = items[:max_items] if max_items else items
    stats["items"] = len(items)
    if not items:
        log_fetch(conn, run_id, source.source_id, "empty", stats)
        return {**stats, "status": "empty"}

    # Страницы скачиваем только для новых URL: уже сохранённые публикации не перекачиваем.
    # Значит, для известного URL без полного текста в RSS сравнивать не с чем — запись не трогаем,
    # иначе полный текст подменился бы анонсом и публикация ложно считалась бы «изменённой».
    known = {i.url for i in items if known_url(conn, i.url)}
    need_page = [i for i in items if source.fulltext == "page" and not i.full_text and i.url not in known]

    def load(item: FeedItem) -> tuple[str, str | None, str | None]:
        try:
            text, date = fetch_article(item.url, download)
            return text, date, None
        except Exception as exc:  # страница недоступна — оставляем анонс из RSS
            return "", None, f"{type(exc).__name__}: {exc}"

    pages: dict[str, tuple[str, str | None, str | None]] = {}
    with ThreadPoolExecutor(max(1, workers)) as pool:
        for item, result in zip(need_page, pool.map(load, need_page)):
            pages[item.url] = result

    for item in items:
        if item.url in known and source.fulltext == "page" and not item.full_text:
            stats["unchanged"] += 1
            continue
        page_text, page_date, page_error = pages.get(item.url, ("", None, None))
        if page_error:
            stats["page_errors"] += 1
        result = _ingest_item(conn, source, item, page_text, page_date)
        stats[result.status] += 1
        if result.status in ("new", "updated"):
            stats[result.prefilter] += 1
            stats["to_process"] += int(result.should_process)

    status = "partial" if stats["page_errors"] else "success"
    log_fetch(conn, run_id, source.source_id, status, stats)
    return {**stats, "status": status}


def fetch_all(conn: sqlite3.Connection, *, sources: list[NewsSource] | None = None, **kwargs: Any) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:12]
    started = now_iso()
    results = [fetch_source(conn, s, run_id=run_id, **kwargs) for s in (sources or enabled_sources())]
    totals = {k: sum(r.get(k, 0) for r in results)
              for k in ("items", "new", "updated", "unchanged", "duplicate", "KEEP", "REVIEW", "DROP", "to_process")}
    return {"run_id": run_id, "started_at": started, "finished_at": now_iso(), "totals": totals, "sources": results}


_MANUAL = NewsSource(MANUAL_SOURCE_ID, "Добавлено вручную", "", "none")


def add_manual_text(conn: sqlite3.Connection, title: str, text: str, *, url: str | None = None,
                    published_at: str | None = None) -> IngestResult:
    """Добавить новость текстом (например, новость, которую жюри даёт для проверки)."""
    # Без URL дубли ищутся по хешу текста.
    item = FeedItem(url=url or "", title=title, full_text=text, published_at=published_at)
    return _ingest_item(conn, _MANUAL, item, "", None, manual=True)


def add_url(conn: sqlite3.Connection, url: str, *, download: Downloader = http_get) -> IngestResult:
    """Добавить новость по ссылке: скачать страницу и выделить текст."""
    raw = download(url).decode("utf-8", "replace")
    text, date = extract_article(raw, url)
    if not text:
        raise FetchError(f"Не удалось выделить текст статьи: {url}")
    title, _, body = text.partition("\n")
    item = FeedItem(url=url, title=title.strip(), full_text=body.strip() or text, published_at=date)
    return _ingest_item(conn, _MANUAL, item, "", date, manual=True)


def enqueue_pending(conn: sqlite3.Connection, queue: Any, *, limit: int | None = None) -> list[str]:
    """Поставить в очередь ai_engine новости, требующие обработки и ещё не поставленные в этой версии.

    ``queue`` — любой ``ai_engine.queue.base.JobQueue`` (RedisQueue, InMemoryQueue).
    """
    from ai_engine.models import JOB_NEWS_PROCESS, NewsJob

    rows = conn.execute(
        """
        SELECT news_id, content_version FROM news
        WHERE should_process = 1 AND (queued_version IS NULL OR queued_version <> content_version)
        ORDER BY published_at DESC
        """ + (" LIMIT ?" if limit else ""),
        (limit,) if limit else (),
    ).fetchall()
    queued = []
    for row in rows:
        job = NewsJob(job_id=f"job_{uuid.uuid4().hex[:12]}", news_id=row["news_id"],
                      job_type=JOB_NEWS_PROCESS, content_version=row["content_version"])
        queue.enqueue(job)
        with conn:
            conn.execute("UPDATE news SET queued_version = ? WHERE news_id = ?", (row["content_version"], row["news_id"]))
        queued.append(row["news_id"])
    return queued
