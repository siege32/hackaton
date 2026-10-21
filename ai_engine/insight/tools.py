"""Explicit agent tools and the hard search budget.

The budget is enforced in code: every analytics/web search call spends one of
at most two steps per company analysis.  A failed tool call also spends a step
so a broken tool can never create a retry loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..errors import ConfigError, SearchLimitExceeded, ToolUnavailableError
from ..storage.base import (
    AnalyticsIndex,
    AnalyticsSnippet,
    CalculationRepository,
    CompanyRepository,
    ExternalSearchTool,
    IntelStore,
    RelationshipRepository,
)


class SearchBudget:
    MAX_ALLOWED = 2

    def __init__(self, max_total: int = 2) -> None:
        if max_total < 0:
            raise ConfigError("search budget cannot be negative")
        if max_total > self.MAX_ALLOWED:
            raise ConfigError(
                f"search budget cannot exceed {self.MAX_ALLOWED} steps per company analysis"
            )
        self.limit = max_total
        self.used = 0
        self.calls: list[str] = []

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def spend(self, tool_name: str) -> None:
        if self.used >= self.limit:
            raise SearchLimitExceeded(
                f"additional search limit of {self.limit} steps reached",
                details={"tool": tool_name},
            )
        self.used += 1
        self.calls.append(tool_name)


@dataclass
class ToolSearchResult:
    analytics: list[AnalyticsSnippet] = field(default_factory=list)
    external: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.analytics and not self.external


class InsightTools:
    """Read-only tools passed to the insight engine."""

    def __init__(
        self,
        *,
        store: IntelStore,
        companies: CompanyRepository,
        relationships: RelationshipRepository,
        analytics: AnalyticsIndex | None,
        calculations: CalculationRepository | None,
        external_search: ExternalSearchTool | None,
        budget: SearchBudget,
    ) -> None:
        self.store = store
        self.companies = companies
        self.relationships = relationships
        self.analytics = analytics
        self.calculations = calculations
        self.external_search = external_search
        self.budget = budget

    def get_profile(self, company_id: str):
        return self.companies.get_company(company_id)

    def get_relationships(self, company_id: str, limit: int = 50):
        return self.relationships.list_relationships(company_id, limit=limit)

    def get_facts(self, news_id: str):
        return self.store.get_facts(news_id)

    def get_assessment(self, news_id: str, company_id: str):
        return self.store.get_assessment(news_id, company_id)

    def get_calculations(self, company_id: str) -> list[dict[str, Any]]:
        if self.calculations is None:
            raise ToolUnavailableError("calculation repository is not configured")
        return self.calculations.list_calculations(company_id)

    def search_analytics(self, query: str, limit: int = 3) -> ToolSearchResult:
        self.budget.spend("bank_analytics")
        if self.analytics is None:
            raise ToolUnavailableError("banking analytics index is not configured")
        snippets = self.analytics.search(query, limit=limit)
        return ToolSearchResult(analytics=list(snippets or []))

    def search_external(self, query: str, limit: int = 3) -> ToolSearchResult:
        self.budget.spend("external_search")
        if self.external_search is None:
            raise ToolUnavailableError("external search tool is not configured")
        results = self.external_search.search(query, limit=limit)
        return ToolSearchResult(external=[r for r in (results or []) if isinstance(r, dict)])
