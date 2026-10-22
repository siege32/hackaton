"""In-memory adapters used by tests and local dry-runs.

They implement exactly the same contracts as the SQL adapters, including the
"save before ack" guarantee: a storage failure raises and nothing is stored.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..errors import StorageError
from ..models import (
    CompanyNewsAssessment,
    CompanyProfile,
    InsightResult,
    NewsFact,
    NewsProcessingResult,
    NewsRecord,
    ProcessingStatus,
    Refinement,
    Relationship,
    RelevanceClass,
)
from ..utils import stable_id, utcnow_iso
from .base import AnalyticsSnippet


def _copy(model):
    return model.__class__.from_dict(deepcopy(model.to_dict()))


class InMemoryNewsRepository:
    def __init__(self, news: list[NewsRecord] | None = None) -> None:
        self._news: dict[str, NewsRecord] = {n.news_id: n for n in (news or [])}

    def add(self, news: NewsRecord) -> None:
        self._news[news.news_id] = news

    def get_news(self, news_id: str) -> NewsRecord | None:
        record = self._news.get(news_id)
        return deepcopy(record) if record else None


class InMemoryCompanyRepository:
    def __init__(self, companies: list[CompanyProfile] | None = None) -> None:
        self._companies: dict[str, CompanyProfile] = {c.company_id: c for c in (companies or [])}

    def add(self, company: CompanyProfile) -> None:
        self._companies[company.company_id] = company

    def get_company(self, company_id: str) -> CompanyProfile | None:
        company = self._companies.get(company_id)
        return _copy(company) if company else None


class InMemoryRelationshipRepository:
    def __init__(self, relationships: list[Relationship] | None = None) -> None:
        self._relationships: dict[str, list[Relationship]] = {}
        for rel in relationships or []:
            self.add(rel)

    def add(self, relationship: Relationship) -> None:
        self._relationships.setdefault(relationship.company_id, []).append(relationship)

    def list_relationships(self, company_id: str, limit: int = 50) -> list[Relationship]:
        return [_copy(r) for r in self._relationships.get(company_id, [])[:limit]]


class InMemoryAnalyticsIndex:
    """Naive token search over caller-provided prepared snippets."""

    def __init__(self, snippets: list[AnalyticsSnippet] | None = None) -> None:
        self._snippets = list(snippets or [])

    def add(self, snippet: AnalyticsSnippet) -> None:
        self._snippets.append(snippet)

    def __len__(self) -> int:
        return len(self._snippets)

    def search(self, query: str, limit: int = 3) -> list[AnalyticsSnippet]:
        terms = {t for t in query.lower().split() if len(t) > 2}
        if not terms:
            return []
        scored: list[tuple[int, AnalyticsSnippet]] = []
        for snippet in self._snippets:
            haystack = f"{snippet.title} {snippet.text}".lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, snippet))
        scored.sort(key=lambda item: item[0], reverse=True)
        results = []
        for score, snippet in scored[:limit]:
            clone = AnalyticsSnippet(
                snippet_id=snippet.snippet_id,
                title=snippet.title,
                text=snippet.text,
                source=snippet.source,
                score=float(score),
                url=snippet.url,
                metadata=deepcopy(snippet.metadata),
            )
            results.append(clone)
        return results


class InMemoryCalculationRepository:
    def __init__(self, calculations: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._calculations = calculations or {}

    def add(self, company_id: str, calculation: dict[str, Any]) -> None:
        self._calculations.setdefault(company_id, []).append(calculation)

    def list_calculations(self, company_id: str) -> list[dict[str, Any]]:
        return deepcopy(self._calculations.get(company_id, []))


class InMemoryIntelStore:
    """Thread-unsafe reference implementation with failure injection."""

    def __init__(self) -> None:
        self.processing: dict[str, NewsProcessingResult] = {}
        self.facts: dict[str, dict[str, NewsFact]] = {}
        self.assessments: dict[tuple[str, str], CompanyNewsAssessment] = {}
        self.assessment_history: list[CompanyNewsAssessment] = []
        self.analyses: dict[str, InsightResult] = {}
        self.refinements: dict[str, list[Refinement]] = {}
        #: Set to True (or a positive integer) in tests to simulate DB outages.
        self.fail_saves = False

    def _check_save(self) -> None:
        if self.fail_saves is True:
            raise StorageError("simulated storage failure")
        if isinstance(self.fail_saves, int) and self.fail_saves > 0:
            self.fail_saves -= 1
            raise StorageError("simulated storage failure")

    # -- news processing -------------------------------------------------
    def begin_processing(
        self,
        news_id: str,
        content_version: str,
        processing_version: str,
        job_id: str | None = None,
    ) -> None:
        self._check_save()
        self.processing[news_id] = NewsProcessingResult(
            news_id=news_id,
            content_version=content_version,
            status=ProcessingStatus.PROCESSING,
            general_relevance=RelevanceClass.REVIEW,
            classification_reason="processing in progress",
            processing_version=processing_version,
            job_id=job_id,
            updated_at=utcnow_iso(),
        )

    def save_processing_result(self, result: NewsProcessingResult) -> None:
        self._check_save()
        self.processing[result.news_id] = _copy(result)
        # Replace facts for the news item: facts from an older content version
        # must never be inherited silently.
        self.facts[result.news_id] = {
            fact.fact_id: _copy(fact) for fact in result.facts
        }

    def get_processing_result(self, news_id: str) -> NewsProcessingResult | None:
        result = self.processing.get(news_id)
        return _copy(result) if result else None

    def get_facts(self, news_id: str, content_version: str | None = None) -> list[NewsFact]:
        facts = list(self.facts.get(news_id, {}).values())
        if content_version is not None:
            facts = [f for f in facts if f.content_version == content_version]
        return [_copy(f) for f in facts]

    # -- company assessments --------------------------------------------
    def save_assessment(self, assessment: CompanyNewsAssessment) -> None:
        self._check_save()
        clone = _copy(assessment)
        self.assessments[(assessment.news_id, assessment.company_id)] = clone
        history = _copy(clone)
        history.assessment_id = stable_id(
            "assesshist", clone.assessment_id, history.updated_at, history.model_id
        )
        self.assessment_history.append(history)

    def get_assessment(self, news_id: str, company_id: str) -> CompanyNewsAssessment | None:
        assessment = self.assessments.get((news_id, company_id))
        return _copy(assessment) if assessment else None

    def list_assessments_for_news(self, news_id: str) -> list[CompanyNewsAssessment]:
        return [
            _copy(a) for (nid, _), a in self.assessments.items() if nid == news_id
        ]

    def list_assessments_for_company(self, company_id: str) -> list[CompanyNewsAssessment]:
        return [_copy(a) for (_, cid), a in self.assessments.items() if cid == company_id]

    # -- insight analyses -----------------------------------------------
    def save_analysis(self, analysis: InsightResult) -> None:
        self._check_save()
        self.analyses[analysis.analysis_id] = _copy(analysis)

    def get_analysis(self, analysis_id: str) -> InsightResult | None:
        analysis = self.analyses.get(analysis_id)
        return _copy(analysis) if analysis else None

    def save_refinement(self, refinement: Refinement) -> None:
        self._check_save()
        self.refinements.setdefault(refinement.analysis_id, []).append(_copy(refinement))

    def list_refinements(self, analysis_id: str) -> list[Refinement]:
        return [_copy(r) for r in self.refinements.get(analysis_id, [])]
