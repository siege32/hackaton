"""Аналитика Альфа-Банка: распознавание PDF-отчётов и поиск по ним (шаг 15 ТЗ).

- ``ocr``     — PDF (сканы без текстового слоя) -> текст по страницам через
                разрешённые LLM с перекрёстной проверкой двумя моделями;
- ``catalog`` — метаданные отчётов: название, дата отчёта (не дата загрузки!);
- ``index``   — фрагменты со страницами и BM25-поиск, адаптер ``AnalyticsIndex``.
"""

from .catalog import DOCUMENTS, ReportDocument, document_by_file
from .index import BankReportsIndex, search_bank_reports

__all__ = ["DOCUMENTS", "BankReportsIndex", "ReportDocument", "document_by_file", "search_bank_reports"]
