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

    @classmethod
    def from_env(cls) -> "DataSettings":
        return cls(
            raw_dir=Path(os.environ.get("DATA_RAW_DIR", "data/raw")),
            db_path=Path(os.environ.get("DATA_DB_PATH", "data/processed/companies.sqlite")),
        )
