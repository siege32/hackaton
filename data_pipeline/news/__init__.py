"""Новости: сбор, очистка, дубли, кластеры событий, предотбор (контур A ТЗ, шаги 1–5).

- ``sources``    — каталог RSS-источников;
- ``fetch``      — загрузка RSS и полного текста статьи (trafilatura);
- ``clean``      — очистка текста и URL;
- ``dedup``      — кластеры событий и «что нового добавила публикация»;
- ``prefilter``  — KEEP / REVIEW / DROP правилами до LLM;
- ``store``      — SQLite-каталог, идемпотентный приём публикаций;
- ``pipeline``   — прогон источников, ручное добавление, постановка в очередь ai_engine;
- ``repository`` — ``NewsRepository`` для ai_engine и инструмент ``search_news``.
"""

from .pipeline import add_manual_text, add_url, enqueue_pending, fetch_all, fetch_source
from .repository import SqliteNewsRepository, cluster_sources, list_news, search_news
from .store import open_news_db

__all__ = [
    "SqliteNewsRepository",
    "add_manual_text",
    "add_url",
    "cluster_sources",
    "enqueue_pending",
    "fetch_all",
    "fetch_source",
    "list_news",
    "open_news_db",
    "search_news",
]
