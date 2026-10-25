from __future__ import annotations

import unittest

from ai_engine.config import ConfigError
from ai_engine.errors import SearchLimitExceeded, ToolUnavailableError
from ai_engine.insight.tools import SearchBudget
from ai_engine.models import (
    AnalysisStatus,
    ImpactDirection,
    ImpactType,
    NewsRecord,
    RefinementKind,
    RelevanceClass,
)
from ai_engine.storage.memory import InMemoryAnalyticsIndex
from ai_engine.storage.base import AnalyticsSnippet
from tests.helpers import (
    GAS_NEWS,
    confirmed_supplier,
    fact_payload,
    make_service,
    payment_link,
)

RELATION_NEWS = NewsRecord(
    news_id="news-relation-1",
    title="Газпром и цены на газ",
    text="Газпром сообщил, что цена на природный газ выросла на 20% в январе 2026 года.",
    source_id="source-1",
    content_version="v1",
)


def snippet(snippet_id="bank-1", text="Цементный завод: анализ рынка газа и издержек.") -> AnalyticsSnippet:
    return AnalyticsSnippet(
        snippet_id=snippet_id,
        title="Отраслевой обзор",
        text=text,
        source="prepared-bank-analytics",
    )


class CountingAnalytics(InMemoryAnalyticsIndex):
    def __init__(self, snippets=None):
        super().__init__(snippets or [])
        self.calls = 0

    def search(self, query, limit=3):
        self.calls += 1
        return super().search(query, limit=limit)


class BrokenAnalytics:
    def __init__(self):
        self.calls = 0

    def search(self, query, limit=3):
        self.calls += 1
        raise ToolUnavailableError("analytics service down")


class InsightEngineTests(unittest.TestCase):
    def _classified(self, news=GAS_NEWS, company_id="company-cement", **kwargs):
        service, store, news_repo, company_repo, rel_repo, llm = make_service(**kwargs)
        news_repo.add(news)
        service.process_news(news.news_id)
        service.classify_news_for_company(news.news_id, company_id)
        return service, store

    def test_analysis_contains_grounded_evidence_chain(self):
        analytics = InMemoryAnalyticsIndex([snippet()])
        service, store = self._classified(analytics=analytics)
        result = service.analyze_company_impact("company-cement", [GAS_NEWS.news_id])
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.relevance_class, RelevanceClass.KEEP)
        self.assertEqual(event.impact_direction, ImpactDirection.NEGATIVE)
        self.assertEqual(event.impact_type, ImpactType.INDIRECT)
        self.assertTrue(event.evidence)
        fact_evidence = [e for e in event.evidence if e.kind == "fact"]
        self.assertTrue(fact_evidence)
        self.assertTrue(fact_evidence[0].quote)
        self.assertTrue(event.chain)
        self.assertTrue(event.potential_consequences)
        self.assertLessEqual(result.additional_searches_used, 2)
        self.assertIn("bank_service_analytics", {s["type"] for s in result.sources_used})

    def test_confirmed_relationship_is_used_as_evidence(self):
        analytics = InMemoryAnalyticsIndex([])
        service, store = self._classified(
            news=RELATION_NEWS,
            analytics=analytics,
            relationships=[confirmed_supplier("company-cement", "Газпром")],
        )
        result = service.analyze_company_impact("company-cement", [RELATION_NEWS.news_id])
        event = result.events[0]
        relationship_evidence = [e for e in event.evidence if e.kind == "relationship"]
        self.assertTrue(relationship_evidence)
        self.assertTrue(relationship_evidence[0].confirmed)
        self.assertIn("relationship", {s["type"] for s in result.sources_used})

    def test_payment_link_is_not_dependency_evidence(self):
        analytics = InMemoryAnalyticsIndex([])
        service, store = self._classified(
            news=RELATION_NEWS,
            analytics=analytics,
            relationships=[payment_link("company-cement", "Газпром")],
        )
        result = service.analyze_company_impact("company-cement", [RELATION_NEWS.news_id])
        event = result.events[0]
        self.assertFalse(
            any(e.kind == "relationship" and e.ref_id == "rel-payment-1" for e in event.evidence)
        )
        self.assertTrue(any("ссылка на оплату" in limitation for limitation in event.limitations))

    def test_missing_analytics_is_not_reported_as_used(self):
        service, store = self._classified(analytics=InMemoryAnalyticsIndex([]))
        result = service.analyze_company_impact("company-cement", [GAS_NEWS.news_id])
        self.assertNotIn("bank_service_analytics", {s["type"] for s in result.sources_used})
        self.assertTrue(any("В банковской аналитике не найдено данных" in item for item in result.unknowns))

    def test_analytics_tool_failure_yields_partial_result_not_loop(self):
        analytics = BrokenAnalytics()
        service, store = self._classified(analytics=analytics)
        result = service.analyze_company_impact("company-cement", [GAS_NEWS.news_id])
        self.assertEqual(result.analysis_status, AnalysisStatus.PARTIAL)
        self.assertEqual(analytics.calls, 1)
        self.assertLessEqual(result.additional_searches_used, 1)
        self.assertTrue(any("analytics" in limitation for limitation in result.limitations))
        self.assertTrue(result.events)

    def test_missing_event_produces_partial_result(self):
        analytics = InMemoryAnalyticsIndex([])
        service, store = self._classified(analytics=analytics)
        result = service.analyze_company_impact(
            "company-cement", [GAS_NEWS.news_id, "news-missing"]
        )
        self.assertEqual(result.analysis_status, AnalysisStatus.PARTIAL)
        self.assertEqual(len(result.events), 1)
        self.assertTrue(any("news-missing" in item for item in result.unknowns))

    def test_all_events_missing_means_failed(self):
        service, *_ = make_service(analytics=InMemoryAnalyticsIndex([]))
        result = service.analyze_company_impact("company-cement", ["news-missing"])
        self.assertEqual(result.analysis_status, AnalysisStatus.FAILED)
        self.assertEqual(result.events, [])

    def test_search_budget_is_hard_limited_to_two(self):
        news_items = [
            NewsRecord(
                news_id=f"news-multi-{i}",
                title="Цены на газ",
                text="Цена на природный газ выросла на 20% в январе 2026 года.",
                source_id="source-1",
                content_version="v1",
            )
            for i in range(3)
        ]
        analytics = CountingAnalytics([snippet("bank-1"), snippet("bank-2"), snippet("bank-3")])
        service, store, news_repo, company_repo, *_ = make_service(
            news=news_items, analytics=analytics
        )
        for item in news_items:
            service.process_news(item.news_id)
            service.classify_news_for_company(item.news_id, "company-cement")
        result = service.analyze_company_impact(
            "company-cement", [item.news_id for item in news_items]
        )
        self.assertLessEqual(analytics.calls, 2)
        self.assertLessEqual(result.additional_searches_used, 2)
        self.assertTrue(any("Лимит дополнительных поисковых запросов" in limitation for limitation in result.limitations))

    def test_budget_utility_raises_on_third_step(self):
        budget = SearchBudget(2)
        budget.spend("a")
        budget.spend("b")
        with self.assertRaises(SearchLimitExceeded):
            budget.spend("c")
        with self.assertRaises(ConfigError):
            SearchBudget(3)

    def test_adaptation_not_relevant_revises_only_affected_event(self):
        analytics = InMemoryAnalyticsIndex([])
        service, store = self._classified(analytics=analytics)
        original = service.analyze_company_impact("company-cement", [GAS_NEWS.news_id])
        adapted = service.adapt_company_analysis(
            original.analysis_id,
            text="Этот риск не относится к компании.",
            kind=RefinementKind.NOT_RELEVANT,
            affected_event_ids=[GAS_NEWS.news_id],
        )
        self.assertEqual(adapted.parent_analysis_id, original.analysis_id)
        self.assertNotEqual(adapted.analysis_id, original.analysis_id)
        self.assertEqual(adapted.events[0].relevance_class, RelevanceClass.DROP)
        self.assertEqual(adapted.events[0].impact_direction, ImpactDirection.UNKNOWN)
        self.assertIn(GAS_NEWS.news_id, adapted.changed_event_ids)
        self.assertEqual(adapted.key_risks, [])
        self.assertTrue(adapted.refinements)
        self.assertEqual(len(store.list_refinements(original.analysis_id)), 1)
        # Original result must remain unchanged.
        still_original = service.get_analysis(original.analysis_id)
        self.assertEqual(still_original.events[0].relevance_class, RelevanceClass.KEEP)

    def test_dependency_refinement_is_marked_as_user_statement(self):
        service, store = self._classified(analytics=InMemoryAnalyticsIndex([]))
        original = service.analyze_company_impact("company-cement", [GAS_NEWS.news_id])
        adapted = service.adapt_company_analysis(
            original.analysis_id,
            text="Компания зависит от Газпрома как ключевого поставщика.",
            kind=RefinementKind.DEPENDENCY,
        )
        event = adapted.events[0]
        user_evidence = [e for e in event.evidence if e.kind == "user_statement"]
        self.assertTrue(user_evidence)
        self.assertFalse(user_evidence[0].confirmed)
        self.assertTrue(event.hypothesis)
        self.assertEqual(event.impact_type, ImpactType.INDIRECT)
        self.assertTrue(any("пользователем" in limitation for limitation in adapted.limitations))

    def test_horizon_refinement_updates_horizon(self):
        service, store = self._classified(analytics=InMemoryAnalyticsIndex([]))
        original = service.analyze_company_impact("company-cement", [GAS_NEWS.news_id])
        adapted = service.adapt_company_analysis(
            original.analysis_id,
            text="Рассматривайте среднесрочный горизонт.",
            kind=RefinementKind.HORIZON,
            horizon="medium_term",
        )
        self.assertEqual(adapted.events[0].impact_horizon.value, "medium_term")
        # News text must never be rewritten by adaptation.
        news = service.news.get_news(GAS_NEWS.news_id)
        self.assertEqual(news.text, GAS_NEWS.text)

    def test_analysis_uses_existing_calculations_as_sources(self):
        service, store = self._classified(analytics=InMemoryAnalyticsIndex([]))
        service.calculations.add(
            "company-cement", {"id": "calc-1", "name": "Gas cost sensitivity", "value": 12}
        )
        result = service.analyze_company_impact("company-cement", [GAS_NEWS.news_id])
        self.assertIn("calculation", {s["type"] for s in result.sources_used})


if __name__ == "__main__":
    unittest.main()
