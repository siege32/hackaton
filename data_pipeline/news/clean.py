"""Очистка текста новости (шаг 2 ТЗ) — обычный код до любых вызовов LLM.

Удаляем только служебное: пробелы/переносы, повторы, подписи вроде
«Новость дополняется», «Подписывайтесь…», выходные данные газеты.
Числа, даты, названия организаций и короткие абзацы не трогаем —
они важны для финансового анализа.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Минимальная длина текста, при которой новость считается полноценной.
MIN_TEXT_CHARS = 200

_TRACKING_PARAMS = re.compile(r"^(utm_|yclid|gclid|fbclid|from$|ref$|rss$|_openstat)", re.I)

#: Строки-«подписи» и интерфейсный мусор, встречающиеся в выдаче trafilatura.
_BOILERPLATE_LINE = re.compile(
    r"^(новость дополняется\.?|подписывайтесь на .*|читайте также.*|читайте нас в .*|"
    r"фото: .*|видео: .*|реклама\.?|erid: .*|газета «коммерсантъ» №.*|от \d{2}\.\d{2}\.\d{4}, стр\. \d+|"
    r"поделиться|материалы по теме:?|.*подпишитесь на наш канал.*)$",
    re.I,
)
_SPACES = re.compile(r"[ \t   ]+")
_INVISIBLE = re.compile(r"[​-‏⁠﻿]")


def normalize_url(url: str) -> str:
    """Нормализованный URL для поиска дублей: без схемы, www, якоря, трекинга и хвостового слэша."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("", host, path, query, ""))[2:]


def normalize_text(text: str) -> str:
    """Единая форма текста: NFC, без невидимых символов, схлопнутые пробелы, абзацы через пустую строку."""
    text = unicodedata.normalize("NFC", html.unescape(text or ""))
    text = _INVISIBLE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    paragraphs: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line:
            continue
        if _BOILERPLATE_LINE.match(line):
            continue
        key = line.lower()
        if key in seen and len(line) > 40:  # повторяющиеся блоки (врезки, подписи)
            continue
        seen.add(key)
        paragraphs.append(line)
    return "\n\n".join(paragraphs)


def content_hash(title: str, text: str) -> str:
    """Хеш очищенного содержимого для точных дублей (регистр и пробелы не важны)."""
    payload = re.sub(r"\s+", " ", f"{title}\n{text}".lower()).strip()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CleanArticle:
    title: str
    lead: str
    body: str
    text: str  # лид + тело: то, что идёт в анализ
    quality: str  # full | short | summary_only

    @property
    def is_full(self) -> bool:
        return self.quality == "full"


def clean_article(title: str, text: str, summary: str = "") -> CleanArticle:
    """Разделить заголовок, лид и основной текст; оценить полноту.

    Если в начале текста повторён заголовок (так делает trafilatura) — убираем.
    Если полного текста нет, используем анонс из RSS и честно помечаем ``summary_only``.
    """
    title = normalize_text(title).replace("\n\n", " ")
    body = normalize_text(text)
    summary = normalize_text(summary)
    if body.lower().startswith(title.lower()):
        body = body[len(title):].lstrip(" .:\n")
    if not body:
        return CleanArticle(title, summary, "", summary, "summary_only")
    paragraphs = body.split("\n\n")
    lead, rest = paragraphs[0], "\n\n".join(paragraphs[1:])
    quality = "full" if len(body) >= MIN_TEXT_CHARS else "short"
    return CleanArticle(title, lead, rest, body, quality)
