"""Command-line entry point.

Usage::

    python -m ai_engine check-config
    python -m ai_engine init-db [--path ./intel.db]
    python -m ai_engine worker [--max-jobs N]

The CLI only reads configuration from the environment (or ``.env`` loaded by
the surrounding deployment).  It never prints the LLM API key.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import Settings
from .errors import ConfigError, IntelError
from .service import build_service
from .storage.sql import SqlStore
from .worker import run_news_worker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sxsvn-intel",
        description="SXSVN AI news-intelligence module",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-config", help="print effective non-secret configuration")

    init_db = sub.add_parser("init-db", help="create the additive intel_* tables")
    init_db.add_argument(
        "--path",
        default=None,
        help="SQLite path (defaults to INTEL_SQLITE_PATH)",
    )

    worker = sub.add_parser("worker", help="consume the existing Redis news queue")
    worker.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="stop after N jobs (default: run until stopped)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = Settings.from_env()

        if args.command == "check-config":
            print(json.dumps(settings.public_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "init-db":
            path = args.path or settings.sqlite_path
            if not path:
                raise ConfigError(
                    "INTEL_SQLITE_PATH is not set; pass --path or configure the variable"
                )
            SqlStore.sqlite(path)
            print(f"intel schema ready: {path}")
            return 0

        service = build_service(settings)
        handled = run_news_worker(service, max_jobs=args.max_jobs)
        print(f"handled {handled} job(s)")
        return 0
    except IntelError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
