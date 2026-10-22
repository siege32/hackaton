"""Загрузка RSS и полного текста статей.

Сетевые функции отделены от разбора: ``parse_rss`` и ``extract_article``
работают с уже скачанными байтами/HTML и тестируются без сети.
"""

from __future__ import annotations

import html
import logging
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

logger = logging.getLogger("data_pipeline.news.fetch")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SXSVN-news-parser/0.1"
TIMEOUT = 20

Downloader = Callable[[str], bytes]


@dataclass
class FeedItem:
    """Одна запись RSS до очистки."""

    url: str
    title: str
    summary: str = ""
    full_text: str = ""
    published_at: str | None = None
    categories: list[str] = field(default_factory=list)
    guid: str = ""


class FetchError(RuntimeError):
    """Источник недоступен или ответ не разобран (а не «пусто»)."""


def http_get(url: str, *, timeout: float = TIMEOUT, retries: int = 2) -> bytes:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500:  # 403/404 повторять бессмысленно
                raise FetchError(f"HTTP {exc.code} {url}") from exc
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        time.sleep(0.5 * (attempt + 1))
    raise FetchError(f"{type(last).__name__}: {last} ({url})") from last


def _text(value: str | None) -> str:
    """HTML-фрагмент из RSS -> текст: убрать теги, раскрыть сущности."""
    if not value:
        return ""
    value = re.sub(r"<br\s*/?>|</p>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return html.unescape(value).strip()


def parse_date(value: str | None) -> str | None:
    """RFC 822 (``Wed, 22 Oct 2026 10:00:00 +0300``) или ISO -> ISO 8601 в UTC.

    Все даты храним в UTC: источники пишут в разных поясах, а сравнение
    ISO-строк (окна кластеров, фильтры периода) корректно только в одном поясе.
    """
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_rss(data: bytes) -> list[FeedItem]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FetchError(f"RSS не разобран: {exc}") from exc
    items = []
    for node in root.iter():
        if _local(node.tag) != "item":
            continue
        fields: dict[str, list[str]] = {}
        for child in node:
            fields.setdefault(_local(child.tag), []).append(child.text or "")
        url = (fields.get("link") or [""])[0].strip()
        title = _text((fields.get("title") or [""])[0])
        if not url or not title:
            continue
        items.append(FeedItem(
            url=url,
            title=title,
            summary=_text((fields.get("description") or fields.get("anons") or [""])[0]),
            full_text=_text((fields.get("full-text") or fields.get("encoded") or [""])[0]),
            published_at=parse_date((fields.get("pubDate") or fields.get("date") or [""])[0]),
            categories=[_text(c) for c in fields.get("category", []) if _text(c)],
            guid=(fields.get("guid") or [""])[0].strip(),
        ))
    return items


def extract_article(page_html: str, url: str | None = None) -> tuple[str, str | None]:
    """Основной текст статьи и дата публикации из HTML (trafilatura).

    Возвращает ("", None), если текст выделить не удалось.
    """
    import trafilatura

    text = trafilatura.extract(
        page_html, url=url, include_comments=False, include_tables=True, favor_precision=True,
    ) or ""
    published = None
    try:
        meta = trafilatura.extract_metadata(page_html, default_url=url)
        published = parse_date(meta.date) if meta and meta.date else None
    except Exception:  # метаданные необязательны
        published = None
    return text.strip(), published


def fetch_article(url: str, download: Downloader = http_get) -> tuple[str, str | None]:
    raw = download(url)
    return extract_article(raw.decode("utf-8", "replace"), url)
