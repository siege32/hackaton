"""Interfaces for external data tools used by the insight engine.

Production integrations should implement ``AgentToolkit`` and return typed
``ToolResult`` values. No synthetic profiles, payments, news, or bank reports
are shipped with the engine.
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import Claim, Evidence, ToolResult

_TOOL_NAMES = frozenset(
    {
        "get_company_profile",
        "get_counterparty_exposure",
        "search_news",
        "search_bank_reports",
        "calculate_exposure",
        "validate_evidence",
    }
)


def tool_names() -> frozenset[str]:
    return _TOOL_NAMES


class AgentToolkit(Protocol):
    def get_company_profile(self, company_id: str) -> ToolResult: ...

    def get_counterparty_exposure(
        self, company_id: str, event_context: dict[str, Any] | None = None
    ) -> ToolResult: ...

    def search_news(
        self, query: str, period: str | None = None, filters: dict[str, Any] | None = None
    ) -> ToolResult: ...

    def search_bank_reports(
        self, topic: str, company_context: dict[str, Any] | None = None
    ) -> ToolResult: ...

    def calculate_exposure(
        self, data: dict[str, Any], parameters: dict[str, Any] | None = None
    ) -> ToolResult: ...

    def validate_evidence(
        self, claims: tuple[Claim, ...], evidence: tuple[Evidence, ...]
    ) -> ToolResult: ...


class UnavailableToolkit:
    """Fail-closed toolkit used when an external integration is not supplied."""

    @staticmethod
    def _error(tool: str) -> ToolResult:
        return ToolResult(
            tool=tool,
            status="error",
            message=f"external tool is not configured: {tool}",
            origin="external",
        )

    def get_company_profile(self, company_id: str) -> ToolResult:
        return self._error("get_company_profile")

    def get_counterparty_exposure(
        self, company_id: str, event_context: dict[str, Any] | None = None
    ) -> ToolResult:
        return self._error("get_counterparty_exposure")

    def search_news(
        self, query: str, period: str | None = None, filters: dict[str, Any] | None = None
    ) -> ToolResult:
        return self._error("search_news")

    def search_bank_reports(
        self, topic: str, company_context: dict[str, Any] | None = None
    ) -> ToolResult:
        return self._error("search_bank_reports")

    def calculate_exposure(
        self, data: dict[str, Any], parameters: dict[str, Any] | None = None
    ) -> ToolResult:
        return self._error("calculate_exposure")

    def validate_evidence(
        self, claims: tuple[Claim, ...], evidence: tuple[Evidence, ...]
    ) -> ToolResult:
        return self._error("validate_evidence")
