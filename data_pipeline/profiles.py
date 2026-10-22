"""Профиль компании (шаг 7 ТЗ) в формате ``ai_engine.models.CompanyProfile``.

Адаптер ``ai_engine.storage.base.CompanyRepository``. Классификатор ai_engine
ищет совпадения тем новости с текстовыми полями профиля (industry, activities,
key_resources, cost_drivers, geography), поэтому поля заполнены
человекочитаемым русским текстом.

Правила:
- в профиль попадает только то, что есть в CSV, или явный вывод по правилу;
  у каждого поля есть запись в ``data_sources`` (файл, колонки, строка или правило);
- пустое поле = «данных нет» (products_services, processes, known_risks
  в кейсе отсутствуют и не выдумываются), список пробелов — в ``raw['missing_data']``;
- контрагенты из платежей — наблюдения: роль «поставщик/покупатель» не подтверждена.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ai_engine.models import CompanyProfile

from . import okved
from .config import COMPANIES_CSV, LARGE_BUSINESS_REVENUE_RUB, PAYMENTS_CSV
from .exposure import CURRENCY, counterparty_flows

_TOP_COUNTERPARTIES = 10

_PRODUCTS = {
    "prod_rko": "расчётно-кассовое обслуживание",
    "prod_gar": "банковские гарантии",
    "prod_loa": "кредитные продукты",
    "prod_lea": "лизинг",
    "prod_dep": "депозиты",
    "prod_zpp": "зарплатный проект",
    "factoring": "факторинг",
}
_PRODUCT_COLUMNS = {
    "prod_rko": "CL_PRODUCT_RKO_MARK",
    "prod_gar": "CL_PRODUCT_GAR_MARK",
    "prod_loa": "CL_PRODUCT_LOA_MARK",
    "prod_lea": "CL_PRODUCT_LEA_MARK",
    "prod_dep": "CL_PRODUCT_DEP_MARK",
    "prod_zpp": "CL_PRODUCT_ZPP_MARK",
    "factoring": "FACTORING_MARK",
}
_CLIENT_TYPES = {
    "CLIENT": "действующий клиент",
    "CLOSE_CLIENT": "клиент с закрытыми продуктами / уходящий",
    "PROSPECT": "потенциальный клиент",
}


def _countries(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [c.strip().capitalize() for c in raw.split(",") if c.strip()]


def _source(field: str, columns: list[str], line: int | None = None, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"field": field, "file": COMPANIES_CSV, "columns": columns}
    if line is not None:
        item["line"] = line
    item.update(extra)
    return item


def _counterparties(flows: list[dict[str, Any]], direction: str) -> list[dict[str, Any]]:
    total = sum(f["amount"] for f in flows)
    role = "получатель исходящих платежей" if direction == "outgoing" else "плательщик входящих платежей"
    return [
        {
            "counterparty_id": f["counterparty_id"],
            "role_basis": f"{role}; роль поставщика/покупателя не подтверждена",
            "industry": f["okved_name"] if f["identified"] else None,
            "okved_section": f["okved_section"] if f["identified"] else None,
            "identified": bool(f["identified"]),
            "amount": f["amount"],
            "share": round(f["amount"] / total, 6) if total else None,
            "payments_count": f["pay_cnt"],
            "source": PAYMENTS_CSV,
            "confirmed": False,
        }
        for f in flows[:_TOP_COUNTERPARTIES]
    ]


def _payment_summary(flows: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(f["amount"] for f in flows)
    unknown = sum(f["amount"] for f in flows if not f["identified"])
    return {
        "total": total,
        "counterparties": len(flows),
        "coverage": round((total - unknown) / total, 6) if total else None,
        "top1_share": round(flows[0]["amount"] / total, 6) if total else None,
    }


def build_profile(conn: sqlite3.Connection, company_id: str) -> CompanyProfile | None:
    row = conn.execute("SELECT * FROM companies WHERE company_id = ?", (company_id,)).fetchone()
    if row is None:
        return None
    c = dict(row)
    line = c["source_line"]
    sources: list[dict[str, Any]] = []
    missing: list[str] = []

    # --- отрасль и деятельность -------------------------------------------------
    industry = okved.SECTION_NAMES.get(c["okved_section"]) if c["okved_section"] else None
    if industry:
        sources.append(_source("industry", ["MAINOKVED_CODE"], line, rule="раздел ОКВЭД 2 по коду"))
    else:
        missing.append("отрасль: код ОКВЭД не распознан")
    activities = []
    if c["okved_name"]:
        activities.append(f"{c['okved_name']} (основной ОКВЭД {c['okved_code']})")
        sources.append(_source("activities", ["MAINOKVED_CODE", "MAINOKVED_NAME"], line))
    missing.append("продукты/услуги компании: нет в данных кейса (только основной ОКВЭД)")
    missing.append("дополнительные виды деятельности: нет в данных кейса")

    # --- финансирование и издержки (из продуктов банка и ФТС) ---------------------
    key_resources: list[str] = []
    cost_drivers: list[str] = []
    if c["prod_loa"]:
        key_resources.append("Заёмное финансирование: кредитные продукты в банке")
    if c["prod_lea"]:
        key_resources.append("Лизинг: часть активов финансируется через лизинг")
    if c["factoring"]:
        key_resources.append("Факторинг: финансирование оборотного капитала под дебиторскую задолженность")
    if c["prod_gar"]:
        key_resources.append("Банковские гарантии")
    if c["prod_loa"] or c["prod_lea"] or c["factoring"]:
        cost_drivers.append(
            # Формулировка важна: классификатор ai_engine ищет основы «ставк», «процент», «кредит».
            "Процентные расходы: стоимость кредитов, лизинга и факторинга зависит от процентной ставки "
            "(в т.ч. ключевой ставки ЦБ)"
        )
        sources.append(_source(
            "key_resources/cost_drivers",
            ["CL_PRODUCT_LOA_MARK", "CL_PRODUCT_LEA_MARK", "FACTORING_MARK", "CL_PRODUCT_GAR_MARK"],
            line,
            rule="наличие продукта финансирования -> зависимость расходов от ставок",
        ))
    countries = _countries(c["fts_countries"])
    if c["fts_flag"]:
        detail = f" (страны по данным ФТС: {', '.join(countries)})" if countries else ""
        cost_drivers.append(
            f"Внешнеэкономическая деятельность: валютный курс, таможенные пошлины и санкционные ограничения{detail}"
        )
        sources.append(_source(
            "cost_drivers", ["FTS_FLAG", "FTS_COUNTRY_NAME"], line,
            rule="есть записи в базе ФТС -> чувствительность к курсу и внешнеторговому регулированию",
        ))
        if not countries:
            missing.append("страны внешнеторговых операций: FTS_FLAG=1, но страны не указаны")
    if not c["prod_loa"] and not c["prod_lea"]:
        missing.append("долговая нагрузка: кредиты в других банках неизвестны (видны только продукты этого банка)")

    # --- география ----------------------------------------------------------------
    geography = [g for g in (c["city"], c["region"]) if g]
    geography += [f"ВЭД: {country}" for country in countries]
    if c["city"] or c["region"]:
        sources.append(_source("geography", ["CITY", "REGION", "FTS_COUNTRY_NAME"], line,
                               note="город/регион регистрации, не обязательно география операций"))
    missing.append("география операций и производственных площадок: известен только адрес регистрации")

    # --- контрагенты из платежей --------------------------------------------------
    outgoing = counterparty_flows(conn, company_id, "outgoing")
    incoming = counterparty_flows(conn, company_id, "incoming")
    suppliers = _counterparties(outgoing, "outgoing")
    customers = _counterparties(incoming, "incoming")
    sources.append({
        "field": "suppliers/customers/financials.payments",
        "file": PAYMENTS_CSV,
        "columns": ["PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"],
        "rule": f"топ-{_TOP_COUNTERPARTIES} контрагентов по сумме; без платежей самой себе",
    })
    if not incoming:
        missing.append("входящие платежи: нет в выгрузке")
    if not outgoing:
        missing.append("исходящие платежи: нет в выгрузке")

    # --- группа компаний ----------------------------------------------------------
    group = None
    if c["group_id"]:
        size, revenue = conn.execute(
            "SELECT COUNT(*), SUM(revenue) FROM companies WHERE group_id = ?", (c["group_id"],)
        ).fetchone()
        group = {"group_id": c["group_id"], "companies_in_directory": size, "revenue_total": revenue}

    revenue = c["revenue"]
    financials = {
        "revenue": {"value": revenue, "currency": CURRENCY, "period": "не указан в данных",
                    "source": f"{COMPANIES_CSV}:REVENUE_COMPANY"},
        "size_segment": None if revenue is None else (
            "крупный бизнес (выручка > 15 млрд)" if revenue >= LARGE_BUSINESS_REVENUE_RUB else "не крупный бизнес"
        ),
        "employees_fns": c["workers_fns"],
        "company_age_years": c["age_gradation"],
        "development_stage": c["eco_stage"],
        "ownership": c["ownership"],
        "client_type": _CLIENT_TYPES.get(c["client_type"] or "", c["client_type"]),
        "bank_products": [name for col, name in _PRODUCTS.items() if c[col]],
        "key_owner_mark": bool(c["key_owner_mark"]) if c["key_owner_mark"] is not None else None,
        "foreign_trade": {"fts_flag": bool(c["fts_flag"]), "countries": countries},
        "group": group,
        "payments": {
            "currency": CURRENCY,
            "period": "не указан в данных",
            "incoming": _payment_summary(incoming),
            "outgoing": _payment_summary(outgoing),
        },
    }
    sources.append(_source(
        "financials",
        ["REVENUE_COMPANY", "AMOUNT_WORKERS_FNS_GRADATION", "COMPANY_AGE_YEARS_GRADATION", "ECO_MARK_LABEL",
         "OKFS_NAME", "CL_TYPE", "NEGATIVEMARK", "GROUP_ID", *_PRODUCT_COLUMNS.values()],
        line,
    ))
    missing.append("рыночная доля и позиция в отрасли: нет в данных кейса")
    missing.append("название компании: данные обезличены")

    return CompanyProfile(
        company_id=company_id,
        name=company_id,
        industry=industry,
        subindustry=c["okved_name"],
        activities=activities,
        key_resources=key_resources,
        cost_drivers=cost_drivers,
        geography=geography,
        suppliers=suppliers,
        customers=customers,
        financials=financials,
        data_sources=sources,
        updated_at=None,  # дата актуальности выгрузки в кейсе не указана
        raw={"csv_row": {k: v for k, v in c.items()}, "missing_data": missing},
    )


class SqliteCompanyRepository:
    """Адаптер ``ai_engine.storage.base.CompanyRepository`` поверх базы ``data_pipeline``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_company(self, company_id: str) -> CompanyProfile | None:
        return build_profile(self.conn, company_id)

    def list_companies(
        self,
        *,
        min_revenue: float | None = LARGE_BUSINESS_REVENUE_RUB,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Краткий список компаний для выбора в интерфейсе (по убыванию выручки)."""
        sql = ["SELECT company_id, okved_code, okved_name, okved_section, revenue, region, client_type",
               "FROM companies WHERE 1=1"]
        params: list[Any] = []
        if min_revenue is not None:
            sql.append("AND revenue >= ?")
            params.append(min_revenue)
        if query:
            sql.append("AND (company_id LIKE ? OR okved_name LIKE ? OR region LIKE ? OR city LIKE ?)")
            params += [f"%{query}%"] * 4
        sql.append("ORDER BY revenue DESC LIMIT ? OFFSET ?")
        params += [limit, offset]
        rows = self.conn.execute(" ".join(sql), params).fetchall()
        return [
            {**dict(r), "industry": okved.SECTION_NAMES.get(r["okved_section"])}
            for r in rows
        ]


def get_company_profile(conn: sqlite3.Connection, company_id: str) -> dict[str, Any]:
    """Инструмент агента ``get_company_profile`` (шаг 14 ТЗ) со статусом."""
    profile = build_profile(conn, company_id)
    if profile is None:
        ambiguous = conn.execute(
            "SELECT rows FROM ambiguous_company_ids WHERE company_id = ?", (company_id,)
        ).fetchone()
        message = (
            f"ID {company_id} неоднозначен: в справочнике {ambiguous[0]} разных компаний с этим ID"
            if ambiguous
            else f"Компания {company_id} не найдена в справочнике"
        )
        return {"tool": "get_company_profile", "status": "empty", "message": message, "data": {}}
    data = profile.to_dict()
    return {
        "tool": "get_company_profile",
        "status": "success",
        "data": data,
        "missing_data": profile.raw["missing_data"],
    }
