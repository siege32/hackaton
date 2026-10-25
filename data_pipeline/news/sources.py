"""Каталог источников новостей (шаг 1 ТЗ).

``fulltext`` — откуда берём полный текст:
- ``rss``  — полный текст есть в самой ленте (РБК: тег ``full-text``);
- ``page`` — скачиваем страницу статьи и вырезаем текст (trafilatura);
- ``none`` — сайт не отдаёт страницы (ТАСС: 403), храним заголовок + анонс из RSS.

Проверено 22.10.2026. ЦБ РФ и Прайм не открываются стандартным набором
сертификатов Python (используют российский корневой сертификат) — отключены.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NewsSource:
    source_id: str
    name: str
    rss_url: str
    fulltext: str  # rss | page | none
    enabled: bool = True
    note: str = ""


SOURCES: tuple[NewsSource, ...] = (
    NewsSource("rbc", "РБК", "https://rssexport.rbc.ru/rbcnews/news/30/full.rss", "rss"),
    NewsSource("interfax", "Интерфакс", "https://www.interfax.ru/rss.asp", "page"),
    NewsSource("kommersant_economics", "Коммерсантъ: Экономика",
               "https://www.kommersant.ru/RSS/section-economics.xml", "page"),
    NewsSource("vedomosti", "Ведомости", "https://www.vedomosti.ru/rss/news", "page"),
    NewsSource("lenta_economics", "Лента.ру: Экономика", "https://lenta.ru/rss/news/economics", "page"),
    NewsSource("banki_ru", "Банки.ру", "https://www.banki.ru/xml/news.rss", "page"),
    NewsSource("tass", "ТАСС", "https://tass.ru/rss/v2.xml", "none",
               note="страницы статей отдают 403 — только заголовок и анонс"),
    NewsSource("cbr", "Банк России", "https://www.cbr.ru/rss/eventrss", "page", enabled=False,
               note="SSL: российский корневой сертификат не входит в стандартный набор"),
)

#: Источник для новостей, добавленных вручную (по ссылке или тексту).
MANUAL_SOURCE_ID = "manual"
MANUAL_SOURCE = NewsSource(MANUAL_SOURCE_ID, "Добавлено вручную", "", "none")

#: Ручной источник тоже ищется по id: иначе в ленте вместо названия виден код «manual».
_BY_ID = {s.source_id: s for s in (*SOURCES, MANUAL_SOURCE)}


def source_by_id(source_id: str) -> NewsSource | None:
    return _BY_ID.get(source_id)


def enabled_sources() -> list[NewsSource]:
    return [s for s in SOURCES if s.enabled]
