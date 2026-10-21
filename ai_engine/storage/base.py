"""Storage and integration interfaces.

The module does not own the news/company tables: those belong to the news
pipeline.  Adapters read them through ``NewsRepository``/``CompanyRepository``.
The module owns only additive ``intel_*`` tables (see ``schema.sql``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from ..models import (
    CompanyNewsAssessment,
    CompanyProfile,
    InsightResult,
    NewsFact,
    NewsProcessingResult,
    NewsRecord,
    Refinement,
    Relationship,
)


@dataclass
class AnalyticsSnippet:
    snippet_id: str
    title: str
    text: str
    source: str = ""
    score: float | None = None
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "snippet_id": self.snippet_id,
            "title": self.title,
            "text": self.text,
            "source": self.source,
            "score": self.score,
            "url": self.url,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class NewsRepository(Protocol):
    def get_news(self, news_id: str) -> NewsRecord | None:
        """Return the current version of a news item or ``None``."""


@runtime_checkable
class CompanyRepository(Protocol):
    def get_company(self, company_id: str) -> CompanyProfile | None:
        """Return the company profile or ``None`` when it does not exist."""


@runtime_checkable
class RelationshipRepository(Protocol):
    def list_relationships(self, company_id: str, limit: int = 50) -> list[Relationship]:
        """Return counterparty edges.  Unconfirmed edges must stay unconfirmed."""


@runtime_checkable
class AnalyticsIndex(Protocol):
    def search(self, query: str, limit: int = 3) -> list[AnalyticsSnippet]:
        """Search prepared banking-analytics material (owned by the data team)."""


@runtime_checkable
class CalculationRepository(Protocol):
    def list_calculations(self, company_id: str) -> list[dict[str, Any]]:
        """Return existing prepared calculations for a company."""


@runtime_checkable
class ExternalSearchTool(Protocol):
    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        """Optional bounded web search.  Results are raw dicts with title/url/snippet."""


@runtime_checkable
class IntelStore(Protocol):
    """Persistence for everything owned by this module."""

    def begin_processing(
        self,
        news_id: str,
        content_version: str,
        processing_version: str,
        job_id: str | None = None,
    ) -> None: ...

    def save_processing_result(self, result: NewsProcessingResult) -> None: ...

    def get_processing_result(self, news_id: str) -> NewsProcessingResult | None: ...

    def get_facts(self, news_id: str, content_version: str | None = None) -> list[NewsFact]: ...

    def save_assessment(self, assessment: CompanyNewsAssessment) -> None: ...

    def get_assessment(self, news_id: str, company_id: str) -> CompanyNewsAssessment | None: ...

    def list_assessments_for_news(self, news_id: str) -> list[CompanyNewsAssessment]: ...

    def save_analysis(self, analysis: InsightResult) -> None: ...

    def get_analysis(self, analysis_id: str) -> InsightResult | None: ...

    def save_refinement(self, refinement: Refinement) -> None: ...

    def list_refinements(self, analysis_id: str) -> list[Refinement]: ...


class HealthCheck(Protocol):
    def ping(self) -> bool: ...


def as_dict_rows(descriptions: Sequence[Any], rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    names = [desc[0] for desc in descriptions]
    return [dict(zip(names, row)) for row in rows]
