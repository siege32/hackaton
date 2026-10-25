"""Импорт CSV кейса в SQLite и отчёт о качестве данных.

Почему SQLite: 1.4 млн платежей не нужно держать в памяти API-процесса,
профиль и экспозиция считаются SQL-запросом по индексу за миллисекунды,
а база пересобирается одной командой из исходных CSV (воспроизводимо).

Правила очистки (каждое отражено в quality_report):
- ID компании, встречающийся в справочнике больше одного раза с разными
  атрибутами, считается неоднозначным: профиль для него не строится,
  платежи с ним трактуются как платежи неидентифицированному контрагенту.
- Платёж компании самой себе (PAYER == BENEFICIARY) исключается: это
  внутренние переводы, они не показывают связь с контрагентами.
- Повторяющиеся пары (PAYER, BENEFICIARY) суммируются.
- Период платежей в данных не указан и не придумывается.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import okved
from .config import COMPANIES_CSV, LARGE_BUSINESS_REVENUE_RUB, PAYMENTS_CSV, PERSON_LINKS_CSV

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE companies (
    company_id      TEXT PRIMARY KEY,
    org_type        TEXT,
    ownership       TEXT,
    age_gradation   TEXT,
    eco_stage       TEXT,
    okved_code      TEXT,
    okved_name      TEXT,
    okved_division  TEXT,
    okved_section   TEXT,
    key_owner_mark  INTEGER,
    workers_fns     TEXT,
    workers_spark   TEXT,
    client_type     TEXT,
    prod_rko        INTEGER,
    prod_gar        INTEGER,
    prod_loa        INTEGER,
    prod_lea        INTEGER,
    prod_dep        INTEGER,
    prod_zpp        INTEGER,
    fts_flag        INTEGER,
    fts_countries   TEXT,
    factoring       INTEGER,
    revenue         REAL,
    group_id        TEXT,
    city            TEXT,
    region          TEXT,
    source_line     INTEGER NOT NULL
);
CREATE INDEX companies_group ON companies(group_id);
CREATE INDEX companies_revenue ON companies(revenue);

CREATE TABLE ambiguous_company_ids (
    company_id   TEXT PRIMARY KEY,
    rows         INTEGER NOT NULL,
    source_lines TEXT NOT NULL
);

CREATE TABLE payments (
    payer        TEXT NOT NULL,
    beneficiary  TEXT NOT NULL,
    amount       REAL NOT NULL,
    pay_cnt      INTEGER NOT NULL,
    source_rows  INTEGER NOT NULL,
    PRIMARY KEY (payer, beneficiary)
) WITHOUT ROWID;
CREATE INDEX payments_beneficiary ON payments(beneficiary);

CREATE TABLE person_links (
    person_id   TEXT NOT NULL,
    company_id  TEXT NOT NULL,
    city        TEXT,
    region      TEXT,
    source_line INTEGER NOT NULL
);
CREATE INDEX person_links_person ON person_links(person_id);
CREATE INDEX person_links_company ON person_links(company_id);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_COMPANY_COLUMNS = {
    # колонка CSV -> колонка таблицы
    "SYNTHETIC_U": "company_id",
    "ORG_TYPE": "org_type",
    "OKFS_NAME": "ownership",
    "COMPANY_AGE_YEARS_GRADATION": "age_gradation",
    "ECO_MARK_LABEL": "eco_stage",
    "MAINOKVED_CODE": "okved_code",
    "MAINOKVED_NAME": "okved_name",
    "NEGATIVEMARK": "key_owner_mark",
    "AMOUNT_WORKERS_FNS_GRADATION": "workers_fns",
    "AMOUNT_WORKERS_SPARK_GRADATION": "workers_spark",
    "CL_TYPE": "client_type",
    "CL_PRODUCT_RKO_MARK": "prod_rko",
    "CL_PRODUCT_GAR_MARK": "prod_gar",
    "CL_PRODUCT_LOA_MARK": "prod_loa",
    "CL_PRODUCT_LEA_MARK": "prod_lea",
    "CL_PRODUCT_DEP_MARK": "prod_dep",
    "CL_PRODUCT_ZPP_MARK": "prod_zpp",
    "FTS_FLAG": "fts_flag",
    "FTS_COUNTRY_NAME": "fts_countries",
    "FACTORING_MARK": "factoring",
    "REVENUE_COMPANY": "revenue",
    "GROUP_ID": "group_id",
    "CITY": "city",
    "REGION": "region",
}
_FLAG_COLUMNS = {
    "key_owner_mark", "prod_rko", "prod_gar", "prod_loa", "prod_lea",
    "prod_dep", "prod_zpp", "fts_flag", "factoring",
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Открыть готовую базу только для чтения (для API и агента)."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(
            f"База данных компаний не найдена: {path}. Соберите её: python -m data_pipeline build"
        )
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _read_csv(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    """Строки CSV с номером строки в файле (заголовок — строка 1)."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for line_no, row in enumerate(csv.DictReader(fh, delimiter=";"), start=2):
            yield line_no, row


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _flag(value: str | None) -> int | None:
    value = _clean(value)
    if value is None:
        return None
    return 1 if float(value) >= 0.5 else 0


def _number(value: str | None) -> float | None:
    value = _clean(value)
    return float(value) if value is not None else None


def build_database(raw_dir: str | Path, db_path: str | Path) -> dict[str, Any]:
    """Собрать базу заново из CSV. Возвращает отчёт о качестве данных."""
    raw_dir, db_path = Path(raw_dir), Path(db_path)
    missing = [name for name in (COMPANIES_CSV, PAYMENTS_CSV, PERSON_LINKS_CSV) if not (raw_dir / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"В папке {raw_dir} нет исходных CSV: {', '.join(missing)}. "
            "Положите туда CSV кейса или возьмите синтетический демо-набор: "
            "python -m data_pipeline --raw-dir data/demo build"
        )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(_SCHEMA)
        report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": {name: str(raw_dir / name) for name in (COMPANIES_CSV, PAYMENTS_CSV, PERSON_LINKS_CSV)},
        }
        report["companies"] = _load_companies(conn, raw_dir / COMPANIES_CSV)
        report["payments"] = _load_payments(conn, raw_dir / PAYMENTS_CSV)
        report["person_links"] = _load_person_links(conn, raw_dir / PERSON_LINKS_CSV)
        report["assumptions"] = [
            "REVENUE_COMPANY и PAY_AMT трактуются как рубли (единица в данных не указана).",
            "Период платежей в данных не указан; показатели — за весь доступный период выгрузки.",
            "Платёж — наблюдение о транзакции, а не доказательство зависимости от контрагента.",
        ]
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?), (?, ?)",
            ("schema_version", SCHEMA_VERSION, "quality_report", json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    tmp_path.replace(db_path)
    return report


def _load_companies(conn: sqlite3.Connection, path: Path) -> dict[str, Any]:
    rows: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    total = 0
    for line_no, raw in _read_csv(path):
        total += 1
        record: dict[str, Any] = {}
        for csv_col, col in _COMPANY_COLUMNS.items():
            value = raw.get(csv_col)
            if col in _FLAG_COLUMNS:
                record[col] = _flag(value)
            elif col == "revenue":
                record[col] = _number(value)
            else:
                record[col] = _clean(value)
        record["okved_code"] = okved.normalize_code(record["okved_code"]) or record["okved_code"]
        record["okved_division"] = okved.division(record["okved_code"])
        record["okved_section"] = okved.section(record["okved_code"])
        record["source_line"] = line_no
        rows.setdefault(record["company_id"], []).append((line_no, record))

    unique, ambiguous = [], []
    for company_id, items in rows.items():
        if len(items) == 1:
            unique.append(items[0][1])
        else:
            ambiguous.append((company_id, len(items), json.dumps([n for n, _ in items])))

    columns = list(unique[0].keys()) if unique else []
    conn.executemany(
        f"INSERT INTO companies({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        ([r[c] for c in columns] for r in unique),
    )
    conn.executemany("INSERT INTO ambiguous_company_ids VALUES (?, ?, ?)", ambiguous)

    stats = conn.execute(
        """
        SELECT COUNT(*),
               SUM(revenue >= ?),
               SUM(okved_section IS NULL),
               SUM(eco_stage IS NULL),
               SUM(workers_spark IS NULL),
               SUM(fts_flag = 1),
               SUM(fts_countries IS NOT NULL)
        FROM companies
        """,
        (LARGE_BUSINESS_REVENUE_RUB,),
    ).fetchone()
    return {
        "rows_in_file": total,
        "companies_loaded": stats[0],
        "large_business_companies": stats[1] or 0,
        "ambiguous_ids": [{"company_id": c, "rows": n} for c, n, _ in ambiguous],
        "ambiguous_rows_excluded": sum(n for _, n, _ in ambiguous),
        "missing": {
            "okved_section_unresolved": stats[2] or 0,
            "eco_stage": stats[3] or 0,
            "workers_spark": stats[4] or 0,
        },
        "fts_flag_set": stats[5] or 0,
        "fts_countries_filled": stats[6] or 0,
    }


def _load_payments(conn: sqlite3.Connection, path: Path) -> dict[str, Any]:
    conn.execute(
        "CREATE TEMP TABLE payments_raw (payer TEXT, beneficiary TEXT, amount REAL, pay_cnt INTEGER)"
    )
    total = self_rows = invalid = 0
    self_amount = 0.0

    def rows():
        nonlocal total, self_rows, invalid, self_amount
        for _, raw in _read_csv(path):
            total += 1
            payer, beneficiary = _clean(raw.get("PAYER")), _clean(raw.get("BENEFICIARY"))
            try:
                amount = float(raw["PAY_AMT"])
                count = int(float(raw["PAY_CNT"]))
            except (KeyError, TypeError, ValueError):
                invalid += 1
                continue
            if not payer or not beneficiary or amount <= 0 or count <= 0:
                invalid += 1
                continue
            if payer == beneficiary:
                self_rows += 1
                self_amount += amount
                continue
            yield payer, beneficiary, amount, count

    conn.executemany("INSERT INTO payments_raw VALUES (?, ?, ?, ?)", rows())
    conn.execute(
        """
        INSERT INTO payments(payer, beneficiary, amount, pay_cnt, source_rows)
        SELECT payer, beneficiary, SUM(amount), SUM(pay_cnt), COUNT(*)
        FROM payments_raw GROUP BY payer, beneficiary
        """
    )
    conn.execute("DROP TABLE payments_raw")

    pairs, merged, amount_total = conn.execute(
        "SELECT COUNT(*), SUM(source_rows > 1), SUM(amount) FROM payments"
    ).fetchone()
    known = conn.execute(
        """
        SELECT
          SUM(CASE WHEN pc.company_id IS NOT NULL AND bc.company_id IS NOT NULL THEN p.amount END),
          SUM(CASE WHEN pc.company_id IS NOT NULL AND bc.company_id IS NOT NULL THEN 1 ELSE 0 END)
        FROM payments p
        LEFT JOIN companies pc ON pc.company_id = p.payer
        LEFT JOIN companies bc ON bc.company_id = p.beneficiary
        """
    ).fetchone()
    return {
        "rows_in_file": total,
        "invalid_rows_skipped": invalid,
        "self_payment_rows_excluded": self_rows,
        "self_payment_amount_excluded": self_amount,
        "pairs_loaded": pairs,
        "duplicate_pairs_merged": merged or 0,
        "amount_total": amount_total or 0.0,
        "pairs_with_both_sides_in_directory": known[1] or 0,
        "amount_share_with_both_sides_in_directory": round((known[0] or 0.0) / amount_total, 4)
        if amount_total
        else None,
    }


def _load_person_links(conn: sqlite3.Connection, path: Path) -> dict[str, Any]:
    total = 0

    def rows():
        nonlocal total
        for line_no, raw in _read_csv(path):
            person, company = _clean(raw.get("SYNTHETIC_F")), _clean(raw.get("SYNTHETIC_U"))
            total += 1
            if person and company:
                yield person, company, _clean(raw.get("CITY_NAME")), _clean(raw.get("RC_NAME")), line_no

    conn.executemany("INSERT INTO person_links VALUES (?, ?, ?, ?, ?)", rows())
    loaded, persons, in_directory, multi = conn.execute(
        """
        SELECT COUNT(*),
               COUNT(DISTINCT person_id),
               SUM(company_id IN (SELECT company_id FROM companies)),
               (SELECT COUNT(*) FROM (SELECT person_id FROM person_links
                                      GROUP BY person_id HAVING COUNT(DISTINCT company_id) > 1))
        FROM person_links
        """
    ).fetchone()
    return {
        "rows_in_file": total,
        "links_loaded": loaded,
        "persons": persons,
        "links_with_company_in_directory": in_directory or 0,
        "persons_linked_to_several_companies": multi,
    }


def quality_report(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM meta WHERE key = 'quality_report'").fetchone()
    return json.loads(row[0]) if row else {}
