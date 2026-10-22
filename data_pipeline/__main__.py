"""CLI модуля данных.

    python -m data_pipeline build                     # CSV -> data/processed/companies.sqlite
    python -m data_pipeline report                    # отчёт о качестве данных
    python -m data_pipeline companies [--all] [-q ..] # список компаний (по умолчанию крупный бизнес)
    python -m data_pipeline profile U_123             # профиль компании (JSON)
    python -m data_pipeline exposure U_123 [--okved B,19]
    python -m data_pipeline relationships U_123 [--limit 20]
    python -m data_pipeline ocr-reports [--doc ID] [--max-pages N] [--max-cost 60]   # платно: LLM
    python -m data_pipeline search-reports "ключевая ставка кредиты" [--limit 3]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from .config import DataSettings, load_dotenv
from .exposure import get_counterparty_exposure
from .profiles import SqliteCompanyRepository, get_company_profile
from .relationships import PaymentRelationshipRepository
from .store import build_database, connect, quality_report


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    settings = DataSettings.from_env()
    parser = argparse.ArgumentParser(prog="python -m data_pipeline", description="Подготовка данных SXSVN")
    parser.add_argument("--raw-dir", default=str(settings.raw_dir), help="папка с исходными CSV")
    parser.add_argument("--db", default=str(settings.db_path), help="путь к SQLite-базе")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("build", help="собрать базу из CSV")
    sub.add_parser("report", help="показать отчёт о качестве данных")
    p = sub.add_parser("companies", help="список компаний")
    p.add_argument("--all", action="store_true", help="не ограничивать крупным бизнесом")
    p.add_argument("-q", "--query", help="поиск по ID, ОКВЭД, региону, городу")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("profile", help="профиль компании")
    p.add_argument("company_id")
    p = sub.add_parser("exposure", help="экспозиция к отраслям через платежи")
    p.add_argument("company_id")
    p.add_argument("--okved", default="", help="разделы/префиксы ОКВЭД через запятую, напр. B,19,46.71")
    p = sub.add_parser("relationships", help="связи компании")
    p.add_argument("company_id")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("ocr-reports", help="распознать PDF аналитики банка (платные вызовы LLM)")
    p.add_argument("--doc", action="append", help="document_id из каталога; можно несколько раз")
    p.add_argument("--max-pages", type=int, help="не больше N первых страниц каждого отчёта")
    p.add_argument("--max-cost", type=float, default=60.0, help="остановиться, если потрачено больше")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--force", action="store_true", help="перераспознать даже закэшированные страницы")
    p.add_argument("--rescore", action="store_true", help="только пересчитать оценки сверки, без LLM")
    p = sub.add_parser("search-reports", help="поиск по аналитике банка")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=3)

    args = parser.parse_args(argv)

    if args.command == "ocr-reports":
        from .reports.ocr import ocr_reports, rescore_pages

        if args.rescore:
            _print({"pages_rescored": rescore_pages(settings.reports_ocr_dir)})
            return 0
        load_dotenv()

        def progress(r):
            extra = f" agreement={r.agreement} {r.confidence}" if r.status == "ok" else (r.error or "")
            print(f"[{r.status}] {r.document_id} стр. {r.page} {r.cost:.3f}{extra}", file=sys.stderr, flush=True)

        _print(ocr_reports(settings.reports_dir, settings.reports_ocr_dir, document_ids=args.doc,
                           max_pages=args.max_pages, max_cost=args.max_cost, workers=args.workers,
                           force=args.force, on_page=progress))
        return 0
    if args.command == "search-reports":
        from .reports.index import BankReportsIndex, search_bank_reports

        _print(search_bank_reports(BankReportsIndex(settings.reports_ocr_dir), args.query, limit=args.limit))
        return 0

    if args.command == "build":
        started = time.monotonic()
        report = build_database(args.raw_dir, args.db)
        report["build_seconds"] = round(time.monotonic() - started, 1)
        _print(report)
        print(f"\nБаза собрана: {args.db}", file=sys.stderr)
        return 0

    conn = connect(args.db)
    if args.command == "report":
        _print(quality_report(conn))
    elif args.command == "companies":
        repo = SqliteCompanyRepository(conn)
        _print(repo.list_companies(min_revenue=None if args.all else 15e9, query=args.query, limit=args.limit))
    elif args.command == "profile":
        result = get_company_profile(conn, args.company_id)
        _print(result)
        return 0 if result["status"] == "success" else 1
    elif args.command == "exposure":
        filters = [f for f in args.okved.split(",") if f.strip()]
        _print(get_counterparty_exposure(conn, args.company_id, filters))
    elif args.command == "relationships":
        rels = PaymentRelationshipRepository(conn).list_relationships(args.company_id, limit=args.limit)
        _print([r.to_dict() for r in rels])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
