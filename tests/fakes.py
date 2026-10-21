from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ai_engine.models import Claim, CompanyProfile, Evidence, NewsItem, ToolResult


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[\w-]+", text.casefold()) if len(token) >= 4}


@dataclass
class FakeToolkit:
    """Test-only external-tool double; production code has no fake data."""

    profiles: dict[str, CompanyProfile] = field(default_factory=lambda: {
        "demo-retail": CompanyProfile(
            company_id="demo-retail", name="Demo Retail", industries=("retail",),
            geographies=("RU",), counterparties=("demo-supplier",), features={"size": "medium"}
        ),
        "demo-logistics": CompanyProfile(
            company_id="demo-logistics", name="Demo Logistics", industries=("logistics",),
            geographies=("RU",), counterparties=("demo-supplier", "demo-carrier"), features={"size": "small"}
        ),
    })
    exposures: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=lambda: {
        "demo-retail": ({"counterparty": "demo-supplier", "amount": 2_300_000, "currency": "RUB", "period": "2026-Q3", "source_id": "payments-retail"},),
        "demo-logistics": ({"counterparty": "demo-supplier", "amount": 800_000, "currency": "RUB", "period": "2026-Q3", "source_id": "payments-logistics"},),
    })
    bank_reports: tuple[dict[str, Any], ...] = ()
    news: tuple[NewsItem, ...] = ()
    unavailable: frozenset[str] = frozenset()

    def _error(self, tool: str) -> ToolResult | None:
        if tool in self.unavailable:
            return ToolResult(tool=tool, status="error", message=f"{tool} unavailable", origin="external")
        return None

    def get_company_profile(self, company_id: str) -> ToolResult:
        if error := self._error("get_company_profile"):
            return error
        profile = self.profiles.get(company_id)
        if profile is None:
            return ToolResult(tool="get_company_profile", status="empty", message="profile not found", origin="external")
        return ToolResult(tool="get_company_profile", status="success", data={"profile": profile.model_dump(mode="json")}, origin="external")

    def get_counterparty_exposure(self, company_id: str, event_context: dict[str, Any] | None = None) -> ToolResult:
        if error := self._error("get_counterparty_exposure"):
            return error
        rows = self.exposures.get(company_id, ())
        participants = tuple((event_context or {}).get("participants", ()))
        rows = tuple(row for row in rows if not participants or any(str(row["counterparty"]).casefold() == participant.casefold() for participant in participants))
        return ToolResult(tool="get_counterparty_exposure", status="success" if rows else "empty", data={"rows": [dict(row) for row in rows]}, message="" if rows else "no matching exposure", origin="external")

    def search_news(self, query: str, period: str | None = None, filters: dict[str, Any] | None = None) -> ToolResult:
        if error := self._error("search_news"):
            return error
        matches = [item for item in self.news if not _tokens(query) or _tokens(query) & _tokens(f"{item.title} {item.text}")]
        return ToolResult(tool="search_news", status="success" if matches else "empty", data={"items": [item.model_dump(mode="json") for item in matches]}, message="" if matches else "news corpus is empty", origin="external")

    def search_bank_reports(self, topic: str, company_context: dict[str, Any] | None = None) -> ToolResult:
        if error := self._error("search_bank_reports"):
            return error
        matches = [item for item in self.bank_reports if _tokens(topic) & _tokens(f"{item['title']} {item['excerpt']} {' '.join(item['topics'])}")]
        return ToolResult(tool="search_bank_reports", status="success" if matches else "empty", data={"documents": matches}, message="" if matches else "no external bank material", origin="external")

    def calculate_exposure(self, data: dict[str, Any], parameters: dict[str, Any] | None = None) -> ToolResult:
        if error := self._error("calculate_exposure"):
            return error
        amount = data.get("amount")
        total = (parameters or {}).get("total_payments")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
            return ToolResult(tool="calculate_exposure", status="partial", data={"amount": amount}, message="amount is not numeric", origin="calculation")
        if isinstance(total, bool) or not isinstance(total, (int, float)) or total <= 0:
            return ToolResult(tool="calculate_exposure", status="partial", data={"amount": amount, "unit": "observed_payments_absolute"}, message="total payments unavailable", origin="calculation")
        return ToolResult(tool="calculate_exposure", status="success", data={"amount": amount, "total_payments": total, "share": round(amount / total, 6), "unit": "share_of_observed_payments", "caveat": "not a revenue share or loss probability"}, origin="calculation")

    def validate_evidence(self, claims: tuple[Claim, ...], evidence: tuple[Evidence, ...]) -> ToolResult:
        evidence_ids = {item.evidence_id for item in evidence}
        issues = [f"claim without evidence: {claim.claim_id}" for claim in claims if not claim.evidence_ids]
        issues.extend(f"claim {claim.claim_id} references unknown evidence" for claim in claims if not set(claim.evidence_ids) <= evidence_ids)
        return ToolResult(tool="validate_evidence", status="success" if not issues else "partial", data={"issues": issues}, message="evidence checked", origin="external")
