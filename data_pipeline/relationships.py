"""Связи компании на один шаг графа (шаг 8 ТЗ: компания -> прямой контрагент).

Адаптер ``ai_engine.storage.base.RelationshipRepository``. Три вида рёбер,
все — наблюдения, а не подтверждённые зависимости (``confirmed=False``):

- ``payment_link`` — платежи между компаниями (df_Uid_payment_hack.csv);
- ``other`` / ``shared_person`` — общее связанное физлицо (df_Fid_Uid_link_hack.csv);
- ``other`` / ``same_group`` — один GROUP_ID в справочнике компаний.

Движок ai_engine не использует такие рёбра как доказательство зависимости и
добавляет ограничение — это ожидаемое поведение. Подтверждать зависимость
(supplier/customer + confirmed=True) можно только по внешнему источнику.
"""

from __future__ import annotations

import sqlite3

from ai_engine.models import Relationship, RelationType, Significance

from .config import COMPANIES_CSV, PAYMENTS_CSV, PERSON_LINKS_CSV
from .exposure import DIRECTIONS, counterparty_flows

#: Пороги доли платежей направления для поля significance (информативно).
_HIGH_SHARE = 0.20
_MEDIUM_SHARE = 0.05
_MAX_GROUP_LINKS = 10
_DIRECTION_RU = {"incoming": "входящих", "outgoing": "исходящих"}
#: Подпись связи для интерфейса (``metadata.label``); значения контракта ai_engine не меняются.
_KIND_LABELS = {
    ("payment", "incoming"): "входящие платежи",
    ("payment", "outgoing"): "исходящие платежи",
    ("shared_person", None): "общие связанные физлица",
    ("same_group", None): "одна группа компаний",
}


def _significance(share: float | None) -> Significance:
    if share is None:
        return Significance.UNKNOWN
    if share >= _HIGH_SHARE:
        return Significance.HIGH
    if share >= _MEDIUM_SHARE:
        return Significance.MEDIUM
    return Significance.LOW


def _label(counterparty_id: str, okved_name: str | None, identified: bool) -> str:
    if not identified:
        return f"{counterparty_id} (нет в справочнике компаний)"
    return f"{counterparty_id} ({okved_name})" if okved_name else counterparty_id


class PaymentRelationshipRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list_relationships(self, company_id: str, limit: int = 50) -> list[Relationship]:
        """Сначала крупнейшие платёжные контрагенты (2/3 лимита), затем структурные связи
        (физлица, группа), затем остальные платёжные.

        Раньше структурные шли первыми, и при небольшом лимите (интерфейс берёт 12)
        список состоял из одних «одна группа компаний» — платёжных контрагентов,
        главного наблюдения о зависимостях, видно не было.
        """
        limit = max(0, limit)
        structural = self._shared_person_links(company_id) + self._group_links(company_id)
        # По доле внутри своего направления: так в начало попадают и главные покупатели,
        # и главные поставщики, а не только сторона с большими суммами.
        payments = sorted(self._payment_links(company_id), key=lambda r: r.share or 0, reverse=True)
        head = payments[: max(1, limit * 2 // 3)] if structural else payments
        rest = payments[len(head):]
        return (head + structural + rest)[:limit]

    # ------------------------------------------------------------------
    def _payment_links(self, company_id: str) -> list[Relationship]:
        links: list[Relationship] = []
        for direction in DIRECTIONS:
            flows = counterparty_flows(self.conn, company_id, direction)
            total = sum(f["amount"] for f in flows)
            for f in flows:
                share = round(f["amount"] / total, 6) if total else None
                links.append(
                    Relationship(
                        relationship_id=f"pay:{company_id}:{direction}:{f['counterparty_id']}",
                        company_id=company_id,
                        counterparty_id=f["counterparty_id"],
                        counterparty_name=_label(f["counterparty_id"], f["okved_name"], bool(f["identified"])),
                        relation_type=RelationType.PAYMENT_LINK,
                        confirmed=False,
                        significance=_significance(share),
                        direction=direction,
                        share=share,
                        evidence=[
                            f"{PAYMENTS_CSV}: {f['pay_cnt']} платежей на сумму {f['amount']:,.0f} "
                            f"({share:.1%} {_DIRECTION_RU[direction]} платежей компании)"
                            if share is not None
                            else f"{PAYMENTS_CSV}: {f['pay_cnt']} платежей"
                        ],
                        source=PAYMENTS_CSV,
                        metadata={
                            "kind": "payment",
                            "label": _KIND_LABELS[("payment", direction)],
                            "amount": f["amount"],
                            "payments_count": f["pay_cnt"],
                            "counterparty_identified": bool(f["identified"]),
                            "counterparty_okved_code": f["okved_code"],
                            "counterparty_okved_section": f["okved_section"],
                            "counterparty_region": f["region"],
                            "period": "период не указан в данных",
                        },
                    )
                )
        links.sort(key=lambda r: r.metadata["amount"], reverse=True)
        return links

    def _shared_person_links(self, company_id: str) -> list[Relationship]:
        rows = self.conn.execute(
            """
            SELECT other.company_id AS counterparty_id,
                   COUNT(DISTINCT other.person_id) AS persons,
                   GROUP_CONCAT(DISTINCT other.person_id) AS person_ids,
                   c.okved_name, c.okved_section,
                   c.company_id IS NOT NULL AS identified
            FROM person_links own
            JOIN person_links other
              ON other.person_id = own.person_id AND other.company_id <> own.company_id
            LEFT JOIN companies c ON c.company_id = other.company_id
            WHERE own.company_id = ?
            GROUP BY other.company_id
            ORDER BY persons DESC, other.company_id
            """,
            (company_id,),
        ).fetchall()
        return [
            Relationship(
                relationship_id=f"person:{company_id}:{r['counterparty_id']}",
                company_id=company_id,
                counterparty_id=r["counterparty_id"],
                counterparty_name=_label(r["counterparty_id"], r["okved_name"], bool(r["identified"])),
                relation_type=RelationType.OTHER,
                confirmed=False,
                direction="none",
                evidence=[f"{PERSON_LINKS_CSV}: общие связанные физлица ({r['persons']}): {r['person_ids']}"],
                source=PERSON_LINKS_CSV,
                metadata={
                    "kind": "shared_person",
                    "label": _KIND_LABELS[("shared_person", None)],
                    "shared_persons": r["persons"],
                    "counterparty_identified": bool(r["identified"]),
                    "counterparty_okved_section": r["okved_section"],
                },
            )
            for r in rows
        ]

    def _group_links(self, company_id: str) -> list[Relationship]:
        rows = self.conn.execute(
            """
            SELECT other.company_id, other.okved_name, other.okved_section, other.revenue, other.group_id
            FROM companies own
            JOIN companies other ON other.group_id = own.group_id AND other.company_id <> own.company_id
            WHERE own.company_id = ?
            ORDER BY other.revenue DESC
            LIMIT ?
            """,
            (company_id, _MAX_GROUP_LINKS),
        ).fetchall()
        return [
            Relationship(
                relationship_id=f"group:{company_id}:{r['company_id']}",
                company_id=company_id,
                counterparty_id=r["company_id"],
                counterparty_name=_label(r["company_id"], r["okved_name"], True),
                relation_type=RelationType.OTHER,
                confirmed=False,
                direction="none",
                evidence=[f"{COMPANIES_CSV}: общий GROUP_ID {r['group_id']}"],
                source=COMPANIES_CSV,
                metadata={
                    "kind": "same_group",
                    "label": _KIND_LABELS[("same_group", None)],
                    "group_id": r["group_id"],
                    "counterparty_okved_section": r["okved_section"],
                    "counterparty_revenue": r["revenue"],
                },
            )
            for r in rows
        ]
