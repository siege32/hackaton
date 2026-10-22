"""Пути к данным. Переопределяются переменными окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

COMPANIES_CSV = "df_Uid_data_hack.csv"
PAYMENTS_CSV = "df_Uid_payment_hack.csv"
PERSON_LINKS_CSV = "df_Fid_Uid_link_hack.csv"

#: Выручка, начиная с которой компания относится к крупному бизнесу (условие кейса).
LARGE_BUSINESS_REVENUE_RUB = 15_000_000_000


@dataclass(frozen=True)
class DataSettings:
    raw_dir: Path
    db_path: Path
    #: PDF аналитики Альфа-Банка (исходники кейса, в git не хранятся).
    reports_dir: Path
    #: Распознанный текст отчётов по страницам (хранится в git — дорогой артефакт).
    reports_ocr_dir: Path

    @classmethod
    def from_env(cls) -> "DataSettings":
        raw = Path(os.environ.get("DATA_RAW_DIR", "data/raw"))
        return cls(
            raw_dir=raw,
            db_path=Path(os.environ.get("DATA_DB_PATH", "data/processed/companies.sqlite")),
            reports_dir=Path(os.environ.get("BANK_REPORTS_DIR", str(raw / "bank_reports"))),
            reports_ocr_dir=Path(os.environ.get("BANK_REPORTS_OCR_DIR", "data/bank_reports/ocr")),
        )


def load_dotenv(path: str | Path = ".env") -> None:
    """Подхватить переменные из .env, не перезаписывая уже заданные в окружении.

    Нужен только для CLI-скриптов (OCR); ai_engine окружение не трогает.
    """
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
