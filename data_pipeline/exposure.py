"""Расчёты по платёжным связям компании (шаг 8 ТЗ).

Экспозиция к затронутой отрасли:

    E = сумма платежей затронутых контрагентов / сумма всех валидных платежей
        того же направления (входящие или исходящие отдельно)

Ограничения, которые возвращаются вместе с числом:
- E — доля наблюдаемых платежей, а не доля выручки и не вероятность убытка;
- контрагенты, которых нет в справочнике компаний, имеют неизвестную отрасль.
  Они не считаются незатронутыми: поэтому возвращается диапазон
  [value, value_upper_bound], где верхняя граница предполагает, что все
  неидентифицированные контрагенты тоже затронуты;
- если знаменатель равен нулю, результат неизвестен (value = None).

Статусы как у инструментов агента: success / partial / empty / error.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Literal

from . import okved
from .config import PAYMENTS_CSV

Direction = Literal["incoming", "outgoing"]
DIRECTIONS: tuple[Direction, ...] = ("incoming", "outgoing")

CURRENCY = "RUB (предположительно; единица в данных не указана)"
PERIOD = "не указан в данных (вся выгрузка)"
_LIMITS = [
    "Доля наблюдаемых платежей в выгрузке банка, а не доля выручки и не вероятность убытка.",
    "Платёж подтверждает транзакцию, но не долгосрочную зависимость от контрагента.",
    "Период платежей в данных не указан.",
]

_DIRECTION_SQL = {
    # (колонка компании, колонка контрагента)
    "incoming": ("beneficiary", "payer"),
    "outgoing": ("payer", "beneficiary"),
}
_DIRECTION_RU = {"incoming": "входящих", "outgoing": "исходящих"}


def counterparty_flows(conn: sqlite3.Connection, company_id: str, direction: Direction) -> list[dict[str, Any]]:
    """Все контрагенты компании в одном направлении, по убыванию суммы."""
    own, other = _DIRECTION_SQL[direction]
    rows = conn.execute(
        f"""
        SELECT p.{other} AS counterparty_id, p.amount, p.pay_cnt,
               c.okved_code, c.okved_name, c.okved_section, c.region,
               c.company_id IS NOT NULL AS identified
        FROM payments p
        LEFT JOIN companies c ON c.company_id = p.{other}
        WHERE p.{own} = ?
        ORDER BY p.amount DESC
        """,
        (company_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _company_exists(conn: sqlite3.Connection, company_id: str) -> bool:
    return conn.execute("SELECT 1 FROM companies WHERE company_id = ?", (company_id,)).fetchone() is not None


def _share(part: float, total: float) -> float | None:
    return round(part / total, 6) if total else None


def calculate_sector_exposure(
    conn: sqlite3.Connection,
    company_id: str,
    okved_filter: Iterable[str],
    direction: Direction,
    *,
    top_n: int = 10,
) -> dict[str, Any]:
    """Доля платежей компании, приходящаяся на контрагентов из затронутых отраслей.

    ``okved_filter`` — буквы разделов ОКВЭД (``'B'``) и/или префиксы кодов
    (``'19'``, ``'46.71'``), описывающие отрасли, которые затрагивает событие.
    """
    filters = [str(f).strip() for f in okved_filter if str(f).strip()]
    calc_id = f"exposure:{company_id}:{direction}:{'+'.join(sorted(filters)) or 'none'}"
    base: dict[str, Any] = {
        "calculation_id": calc_id,
        "name": f"Доля {_DIRECTION_RU.get(direction, direction)} платежей от затронутых отраслей",
        "company_id": company_id,
        "direction": direction,
        "okved_filter": filters,
        "formula": (
            "E = сумма платежей контрагентов из затронутых отраслей / "
            f"сумма всех валидных {_DIRECTION_RU.get(direction, direction)} платежей"
        ),
        "unit": "доля от 0 до 1",
        "currency": CURRENCY,
        "period": PERIOD,
        "inputs": {
            "source_file": PAYMENTS_CSV,
            "table": "payments",
            "excluded": ["платежи компании самой себе"],
        },
        "limitations": list(_LIMITS),
    }
    if direction not in DIRECTIONS:
        return {**base, "status": "error", "message": f"Неизвестное направление: {direction}"}
    if not filters:
        return {**base, "status": "error", "message": "Не задан фильтр затронутых отраслей (okved_filter)"}
    if not _company_exists(conn, company_id):
        return {**base, "status": "error", "message": f"Компания {company_id} не найдена в справочнике"}

    flows = counterparty_flows(conn, company_id, direction)
    denominator = sum(f["amount"] for f in flows)
    if not flows or denominator <= 0:
        return {
            **base,
            "status": "empty",
            "value": None,
            "message": f"У компании нет {_DIRECTION_RU[direction]} платежей в выгрузке — показатель неизвестен",
        }

    affected = [f for f in flows if f["identified"] and okved.matches(f["okved_code"], filters)]
    unidentified = [f for f in flows if not f["identified"]]
    numerator = sum(f["amount"] for f in affected)
    unidentified_amount = sum(f["amount"] for f in unidentified)
    coverage = _share(denominator - unidentified_amount, denominator)

    result = {
        **base,
        "status": "success" if not unidentified else "partial",
        "value": _share(numerator, denominator),
        "value_upper_bound": _share(numerator + unidentified_amount, denominator),
        "numerator": numerator,
        "denominator": denominator,
        "coverage": coverage,
        "unidentified_amount": unidentified_amount,
        "counterparties_total": len(flows),
        "counterparties_affected": len(affected),
        "counterparties_unidentified": len(unidentified),
        "affected_counterparties": [
            {
                "counterparty_id": f["counterparty_id"],
                "amount": f["amount"],
                "share": _share(f["amount"], denominator),
                "payments_count": f["pay_cnt"],
                "okved_code": f["okved_code"],
                "okved_name": f["okved_name"],
            }
            for f in affected[:top_n]
        ],
    }
    if unidentified:
        result["limitations"].append(
            f"{len(unidentified)} контрагентов ({_pct(unidentified_amount, denominator)} суммы) нет в справочнике: "
            "их отрасль неизвестна, они не считаются незатронутыми — см. value_upper_bound."
        )
    return result


def sector_breakdown(conn: sqlite3.Connection, company_id: str, direction: Direction) -> dict[str, Any]:
    """Структура платежей по разделам ОКВЭД контрагентов + доля неидентифицированных."""
    flows = counterparty_flows(conn, company_id, direction)
    total = sum(f["amount"] for f in flows)
    calc_id = f"breakdown:{company_id}:{direction}"
    base = {
        "calculation_id": calc_id,
        "name": f"Структура {_DIRECTION_RU[direction]} платежей по отраслям контрагентов",
        "company_id": company_id,
        "direction": direction,
        "formula": "доля раздела = сумма платежей контрагентов раздела / сумма всех платежей направления",
        "unit": "доля от 0 до 1",
        "currency": CURRENCY,
        "period": PERIOD,
        "inputs": {"source_file": PAYMENTS_CSV, "table": "payments", "excluded": ["платежи компании самой себе"]},
        "limitations": list(_LIMITS),
    }
    if not flows or total <= 0:
        return {**base, "status": "empty", "total": 0.0, "sectors": [], "message": "Платежей нет"}

    buckets: dict[str, dict[str, Any]] = {}
    for f in flows:
        key = f["okved_section"] if f["identified"] and f["okved_section"] else "unknown"
        bucket = buckets.setdefault(
            key,
            {
                "okved_section": None if key == "unknown" else key,
                "section_name": okved.SECTION_NAMES.get(key, "Контрагент не идентифицирован / отрасль неизвестна"),
                "amount": 0.0,
                "counterparties": 0,
            },
        )
        bucket["amount"] += f["amount"]
        bucket["counterparties"] += 1
    sectors = sorted(buckets.values(), key=lambda b: b["amount"], reverse=True)
    for b in sectors:
        b["share"] = _share(b["amount"], total)
    unknown = buckets.get("unknown", {}).get("amount", 0.0)
    top = flows[:5]
    return {
        **base,
        "status": "partial" if unknown else "success",
        "total": total,
        "coverage": _share(total - unknown, total),
        "counterparties_total": len(flows),
        "top1_share": _share(top[0]["amount"], total),
        "top5_share": _share(sum(f["amount"] for f in top), total),
        "sectors": sectors,
    }


def get_counterparty_exposure(
    conn: sqlite3.Connection,
    company_id: str,
    okved_filter: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Инструмент агента ``get_counterparty_exposure`` (шаг 14 ТЗ).

    Без фильтра — структура платежей по отраслям в обе стороны.
    С фильтром — экспозиция к затронутым отраслям во входящих и исходящих платежах.
    """
    if not _company_exists(conn, company_id):
        return {"tool": "get_counterparty_exposure", "status": "error",
                "message": f"Компания {company_id} не найдена в справочнике", "data": {}}
    filters = [f for f in (okved_filter or []) if str(f).strip()]
    if filters:
        data = {d: calculate_sector_exposure(conn, company_id, filters, d) for d in DIRECTIONS}
    else:
        data = {d: sector_breakdown(conn, company_id, d) for d in DIRECTIONS}
    statuses = {v["status"] for v in data.values()}
    if statuses == {"empty"}:
        status = "empty"
    elif statuses <= {"success"}:
        status = "success"
    elif "error" in statuses:
        status = "error"
    else:
        status = "partial"
    return {"tool": "get_counterparty_exposure", "status": status, "data": data}


class PaymentCalculationRepository:
    """Адаптер ``ai_engine.storage.base.CalculationRepository``.

    Возвращает базовые расчёты, не зависящие от события: структура входящих
    и исходящих платежей по отраслям контрагентов и концентрация.
    Расчёты под конкретное событие — ``calculate_sector_exposure``.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list_calculations(self, company_id: str) -> list[dict[str, Any]]:
        if not _company_exists(self.conn, company_id):
            return []
        return [
            calc
            for calc in (sector_breakdown(self.conn, company_id, d) for d in DIRECTIONS)
            if calc["status"] != "empty"
        ]


def _pct(part: float, total: float) -> str:
    return f"{part / total:.1%}" if total else "н/д"
