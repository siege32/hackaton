from __future__ import annotations

import unittest
from types import SimpleNamespace

from ai_engine.config import Settings
from ai_engine.facts import FactExtractionService, HeuristicFactExtractor, LLMFactExtractor
from ai_engine.llm import LLMError, OpenAICompatibleClient
from ai_engine.models import CompanyProfile, EventFact, NewsItem
from ai_engine.relevance import LLMRelevanceAssessor, RuleBasedRelevanceAssessor
from ai_engine.validation import validate_result
from ai_engine.models import AnalysisContext
from tests.fakes import FakeToolkit


def profile(**overrides) -> CompanyProfile:
    values = {
        "company_id": "demo-retail",
        "name": "Demo Retail",
        "industries": ("retail",),
        "geographies": ("RU",),
        "counterparties": ("demo-supplier",),
    }
    values.update(overrides)
    return CompanyProfile(**values)


def extraction(**overrides):
    from ai_engine.models import EventExtraction, EventFact

    values = {
        "event_id": "n1",
        "topic": "supplier outage",
        "facts": (
            EventFact(fact_id="f1", text="Supplier announced an outage.", source_ids=("s1",), verified=True),
        ),
    }
    values.update(overrides)
    return EventExtraction(**values)


class ExternalToolTests(unittest.TestCase):
    def test_calculate_exposure_success_and_partial(self):
        toolkit = FakeToolkit()
        success = toolkit.calculate_exposure({"amount": 2_300_000}, {"total_payments": 10_000_000})
        self.assertEqual(success.status, "success")
        self.assertAlmostEqual(success.data["share"], 0.23)
        self.assertIn("not a revenue share", success.data["caveat"])

        partial = toolkit.calculate_exposure({"amount": 2_300_000}, {})
        self.assertEqual(partial.status, "partial")
        self.assertNotIn("share", partial.data)

    def test_empty_and_error_are_distinguishable(self):
        empty = FakeToolkit(bank_reports=()).search_bank_reports("supplier outage", {})
        error = FakeToolkit(unavailable=frozenset({"search_bank_reports"})).search_bank_reports(
            "supplier outage", {}
        )
        self.assertEqual(empty.status, "empty")
        self.assertEqual(error.status, "error")
        self.assertTrue(empty.message)
        self.assertTrue(error.message)

    def test_news_search_distinguishes_empty_corpus_from_error(self):
        self.assertEqual(FakeToolkit().search_news("supplier").status, "empty")
        self.assertEqual(
            FakeToolkit(unavailable=frozenset({"search_news"})).search_news("supplier").status,
            "error",
        )

    def test_company_profile_lookup(self):
        self.assertEqual(FakeToolkit().get_company_profile("demo-retail").status, "success")
        self.assertEqual(FakeToolkit().get_company_profile("unknown").status, "empty")


class LLMAdapterTests(unittest.TestCase):
    def _client(self, choices):
        class Completions:
            def create(self, **kwargs):
                return SimpleNamespace(choices=choices)

        class FakeOpenAI:
            def __init__(self):
                self.chat = SimpleNamespace(completions=Completions())

        settings = Settings(_env_file=None, SXSVN_LLM_API_KEY="sk-test")
        return OpenAICompatibleClient(settings, client=FakeOpenAI())

    def test_valid_json_object(self):
        choice = SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))
        self.assertEqual(self._client([choice]).complete([]), {"ok": True})

    def test_invalid_json_raises_safe_error(self):
        choice = SimpleNamespace(message=SimpleNamespace(content="not json"))
        with self.assertRaises(LLMError):
            self._client([choice]).complete([])

    def test_non_object_json_is_rejected(self):
        choice = SimpleNamespace(message=SimpleNamespace(content="[]"))
        with self.assertRaises(LLMError):
            self._client([choice]).complete([])

    def test_empty_choices_raise_safe_error(self):
        with self.assertRaises(LLMError):
            self._client([]).complete([])


class FactExtractionTests(unittest.TestCase):
    def test_heuristic_extraction_keeps_dates_separate(self):
        news = NewsItem(
            news_id="n1",
            title="Supplier outage",
            text="Supplier A announced an outage. Deliveries may be delayed.",
            source_id="s1",
            published_at="2026-10-01",
        )
        result = HeuristicFactExtractor().extract(news, "e1")
        self.assertEqual(result.extractor, "heuristic")
        self.assertIsNone(result.occurred_at)
        self.assertEqual(result.published_at, "2026-10-01")
        self.assertTrue(result.facts)
        self.assertTrue(all(fact.source_ids == ("s1",) for fact in result.facts))
        self.assertTrue(all(fact.occurred_at is None for fact in result.facts))
        self.assertTrue(all(fact.verified is False for fact in result.facts))

    def test_llm_extraction_fills_missing_source_ids(self):
        class StubLLM:
            def complete(self, messages):
                return {
                    "event_id": "n1",
                    "topic": "supplier outage",
                    "facts": [
                        {"fact_id": "f1", "text": "Supplier A announced an outage.", "source_ids": [], "verified": True}
                    ],
                    "source_ids": ["s1"],
                }

        news = NewsItem(news_id="n1", text="Supplier A announced an outage.", source_id="s1")
        result = LLMFactExtractor(StubLLM()).extract(news, "e1")
        self.assertEqual(result.extractor, "llm")
        self.assertEqual(result.facts[0].source_ids, ("s1",))

    def test_extraction_service_caches_by_news(self):
        calls = {"count": 0}

        class CountingExtractor:
            name = "counting"

            def extract(self, news, event_id):
                calls["count"] += 1
                return extraction()

        service = FactExtractionService(CountingExtractor())
        news = NewsItem(news_id="n1", text="Supplier A announced an outage.", source_id="s1")
        first = service.extract(news, "e1")
        second = service.extract(news, "e2")
        self.assertIs(first, second)
        self.assertEqual(calls["count"], 1)


class RelevanceTests(unittest.TestCase):
    def test_direct_participant(self):
        result = RuleBasedRelevanceAssessor().assess(profile(), extraction(participants=("Demo Retail",)))
        self.assertEqual((result.decision, result.channel), ("KEEP", "direct"))

    def test_counterparty_link(self):
        result = RuleBasedRelevanceAssessor().assess(profile(), extraction(participants=("demo-supplier",)))
        self.assertEqual((result.decision, result.channel), ("KEEP", "counterparty"))
        self.assertTrue(any("not proof" in base or "not confirmed" in base for base in (*result.bases, *result.missing_data)))

    def test_industry_only_is_review_not_drop(self):
        result = RuleBasedRelevanceAssessor().assess(
            profile(), extraction(participants=("someone-else",), industries=("retail",))
        )
        self.assertEqual((result.decision, result.channel), ("REVIEW", "industry"))

    def test_geography_only_is_review(self):
        result = RuleBasedRelevanceAssessor().assess(
            profile(), extraction(participants=("someone-else",), geographies=("RU",))
        )
        self.assertEqual((result.decision, result.channel), ("REVIEW", "geography"))

    def test_unknown_metadata_is_review(self):
        result = RuleBasedRelevanceAssessor().assess(profile(), extraction())
        self.assertEqual(result.decision, "REVIEW")
        self.assertTrue(result.missing_data)

    def test_unrelated_event_is_drop(self):
        result = RuleBasedRelevanceAssessor().assess(
            profile(),
            extraction(participants=("oil-co",), industries=("energy",), geographies=("KZ",)),
        )
        self.assertEqual(result.decision, "DROP")

    def test_llm_relevance_assessor_uses_model_payload(self):
        class StubLLM:
            def complete(self, messages):
                return {
                    "decision": "KEEP",
                    "score": 0.8,
                    "rationale": "Named participant.",
                    "bases": ["participant"],
                    "missing_data": [],
                    "channel": "direct",
                }

        result = LLMRelevanceAssessor(StubLLM()).assess(profile(), extraction(participants=("demo-retail",)))
        self.assertEqual((result.decision, result.channel), ("KEEP", "direct"))

    def test_llm_relevance_assessor_falls_back_on_bad_payload(self):
        class BadLLM:
            def complete(self, messages):
                return {"decision": "MAYBE"}

        result = LLMRelevanceAssessor(BadLLM()).assess(
            profile(), extraction(participants=("demo-supplier",))
        )
        self.assertEqual((result.decision, result.channel), ("KEEP", "counterparty"))


class ValidationTests(unittest.TestCase):
    def _context(self, **overrides) -> AnalysisContext:
        values = {
            "company_id": "demo-retail",
            "event_id": "e1",
            "as_of_date": "2026-10-21",
            "profile": profile(),
            "event_facts": (EventFact(fact_id="f1", text="Outage.", source_ids=("s1",)),),
        }
        values.update(overrides)
        return AnalysisContext(**values)

    def test_invalid_date_and_amount_are_flagged(self):
        from ai_engine.models import AnalysisResult, Evidence

        ctx = self._context(
            as_of_date="21.10.2026",
            event_facts=(
                EventFact(
                    fact_id="f1",
                    text="Outage.",
                    source_ids=("s1",),
                    value={"amount": "many", "unit": ""},
                ),
            ),
        )
        result = AnalysisResult(
            event={"event_id": "e1"},
            priority={},
            company_features={},
            bank_view={},
            ai_implications=(),
            risks=(),
            opportunities=(),
            missing_data=(),
            next_check=None,
            evidence=(Evidence(evidence_id="ev-1", source_id="s1", quote="Outage.", kind="event"),),
            trace_id="t1",
            status="completed",
        )
        codes = {issue.code for issue in validate_result(result, ctx)}
        self.assertIn("invalid_date", codes)
        self.assertIn("invalid_value", codes)
        self.assertIn("invalid_unit", codes)

    def test_source_allow_list_is_enforced(self):
        from ai_engine.models import AnalysisResult, Evidence, Source

        ctx = self._context(sources=(Source(source_id="s9", title="Other"),))
        result = AnalysisResult(
            event={"event_id": "e1"},
            priority={},
            company_features={},
            bank_view={},
            ai_implications=(),
            risks=(),
            opportunities=(),
            missing_data=(),
            next_check=None,
            evidence=(Evidence(evidence_id="ev-1", source_id="s1", quote="Outage.", kind="event"),),
            trace_id="t1",
            status="completed",
        )
        codes = {issue.code for issue in validate_result(result, ctx)}
        self.assertIn("source_not_declared", codes)
        self.assertIn("unknown_source", codes)


if __name__ == "__main__":
    unittest.main()
