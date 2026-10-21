from __future__ import annotations

import unittest
from dataclasses import dataclass

from ai_engine.agent import InsightEngine as ProductionInsightEngine
from ai_engine.insight_engine import insight_engine
from ai_engine.models import (
    AnalysisContext,
    BankEvidence,
    CompanyProfile,
    EventExtraction,
    EventFact,
    NewsItem,
)
from tests.fakes import FakeToolkit


class InsightEngine(ProductionInsightEngine):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("toolkit", FakeToolkit())
        super().__init__(*args, **kwargs)


def profile(company_id: str, **overrides) -> CompanyProfile:
    defaults = {
        "company_id": company_id,
        "name": company_id.replace("-", " ").title(),
        "industries": ("retail",),
        "geographies": ("RU",),
        "counterparties": ("demo-supplier",),
        "features": {"size": "medium"},
    }
    defaults.update(overrides)
    return CompanyProfile(**defaults)


def fact(
    text: str = "Demo Supplier announced a production outage.",
    *,
    fact_id: str = "f1",
    source: str = "s1",
    **overrides,
) -> EventFact:
    values = {
        "fact_id": fact_id,
        "text": text,
        "source_ids": (source,),
        "verified": True,
        "topic": "supplier outage",
    }
    values.update(overrides)
    return EventFact(**values)


def context(
    *,
    profile_obj: CompanyProfile | None = None,
    facts: tuple[EventFact, ...] | None = None,
    bank: bool = False,
    calculations: dict | None = None,
    news: NewsItem | None = None,
) -> AnalysisContext:
    profile_obj = profile_obj or profile("demo-retail")
    return AnalysisContext(
        company_id=profile_obj.company_id,
        event_id="e1",
        as_of_date="2026-10-21",
        profile=profile_obj,
        event_facts=(fact(),) if facts is None else facts,
        bank_evidence=(
            (BankEvidence(source_id="b1", text="Observed payments to the supplier."),) if bank else ()
        ),
        calculations=calculations or {},
        news=news,
    )


class StubExtractor:
    """Returns a fixed extraction and counts calls, simulating a model extractor."""

    name = "stub"

    def __init__(self, extraction: EventExtraction):
        self.extraction = extraction
        self.calls = 0

    def extract(self, news: NewsItem, event_id: str) -> EventExtraction:
        self.calls += 1
        return self.extraction.model_copy(update={"event_id": event_id})


@dataclass
class CountingNewsToolkit(FakeToolkit):
    news_calls: int = 0

    def search_news(self, query: str, period: str | None = None, filters: dict | None = None):
        self.news_calls += 1
        return super().search_news(query, period, filters)


class AgentScenarioTests(unittest.TestCase):
    def test_1_direct_impact(self):
        ctx = context(
            facts=(
                fact(
                    "Demo Retail reports a fire at its warehouse.",
                    participants=("demo-retail",),
                    topic="warehouse fire",
                ),
            ),
            bank=True,
        )
        result = InsightEngine().analyze(ctx)
        self.assertEqual(result.relevance["decision"], "KEEP")
        self.assertEqual(result.relevance["channel"], "direct")
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.ai_implications)
        self.assertTrue(all(claim.evidence_ids for claim in result.ai_implications))
        self.assertTrue(result.ai_implications[0].mechanism)
        self.assertTrue(result.trace_id)

    def test_2_indirect_impact_through_counterparty(self):
        ctx = context(
            facts=(
                fact(
                    participants=("demo-supplier",),
                    topic="supplier outage",
                ),
            ),
            calculations={"total_outgoing_payments": 10_000_000},
        )
        result = InsightEngine().analyze(ctx)
        self.assertEqual(result.relevance["decision"], "KEEP")
        self.assertEqual(result.relevance["channel"], "counterparty")
        exposure = next(claim for claim in result.ai_implications if claim.claim_id == "counterparty-exposure")
        self.assertIn("payment share", exposure.limitation.lower())
        self.assertIn("calc-payment-share", exposure.evidence_ids)
        self.assertTrue(result.risks)
        self.assertEqual(result.evidence_quality["level"], "medium")
        self.assertTrue(any("bank_material_not_found" in note for note in result.missing_data))

    def test_3_irrelevant_event_has_no_invented_link(self):
        ctx = context(
            facts=(
                fact(
                    "An oil producer reported an outage.",
                    participants=("oil-co",),
                    industries=("energy",),
                    geographies=("KZ",),
                    topic="oil outage",
                ),
            ),
        )
        result = InsightEngine().analyze(ctx)
        self.assertEqual(result.relevance["decision"], "DROP")
        self.assertEqual(result.ai_implications, ())
        self.assertEqual(result.risks, ())
        self.assertEqual(result.opportunities, ())
        self.assertEqual(result.status, "needs_review")
        self.assertTrue(any(note.startswith("event_dropped:") for note in result.missing_data))

    def test_4_insufficient_data_is_explicit(self):
        ctx = context(
            facts=(fact(participants=("demo-retail",), topic="hr policy"),),
        )
        result = InsightEngine().analyze(ctx)
        self.assertEqual(result.relevance["decision"], "KEEP")
        self.assertEqual(result.status, "needs_review")
        self.assertIsNotNone(result.next_check)
        self.assertTrue(any(note.startswith("bank_material_not_found:") for note in result.missing_data))
        self.assertEqual(result.evidence_quality["level"], "low")

    def test_5_contradictory_sources_are_flagged(self):
        ctx = context(
            bank=True,
            facts=(
                fact(fact_id="f1", source="s1", topic="outage_duration", value={"hours": 3}),
                fact(
                    "A second source reports a much longer outage.",
                    fact_id="f2",
                    source="s2",
                    topic="outage_duration",
                    value={"hours": 7},
                ),
            ),
        )
        result = InsightEngine().analyze(ctx)
        self.assertEqual(result.status, "needs_review")
        self.assertTrue(any(note.startswith("contradictory_sources:") for note in result.missing_data))

    def test_6_one_news_two_companies_different_bases(self):
        extraction = EventExtraction(
            event_id="n1",
            topic="supplier outage",
            participants=("demo-retail", "demo-supplier"),
            industries=("manufacturing",),
            facts=(
                fact(
                    "Demo Supplier announced an outage affecting Demo Retail.",
                    participants=("demo-supplier",),
                    verified=True,
                ),
            ),
            extractor="heuristic",
        )
        extractor = StubExtractor(extraction)
        news = NewsItem(news_id="n1", text="Demo Supplier announced an outage.", source_id="s1")
        engine = InsightEngine(fact_extractor=extractor)

        retail = engine.analyze(context(profile_obj=profile("demo-retail"), facts=(), news=news))
        logistics = engine.analyze(
            context(
                profile_obj=profile("demo-logistics", industries=("logistics",)),
                facts=(),
                news=news,
            )
        )

        self.assertEqual(retail.relevance["channel"], "direct")
        self.assertEqual(logistics.relevance["channel"], "counterparty")
        self.assertNotEqual(retail.relevance["rationale"], logistics.relevance["rationale"])
        self.assertEqual(extractor.calls, 1)

    def test_7_repeat_analysis_does_not_re_extract(self):
        extraction = EventExtraction(
            event_id="n1",
            topic="supplier outage",
            participants=("demo-retail",),
            facts=(fact("Demo Supplier announced an outage."),),
        )
        extractor = StubExtractor(extraction)
        engine = InsightEngine(fact_extractor=extractor)
        ctx = context(facts=(), news=NewsItem(news_id="n1", text="Demo Supplier announced an outage.", source_id="s1"))

        first = engine.analyze(ctx)
        second = engine.analyze(ctx)

        self.assertIs(first, second)
        self.assertEqual(extractor.calls, 1)
        self.assertFalse(any("unknown_source" in note for note in first.missing_data))

    def test_8_unavailable_tool_differs_from_empty_result(self):
        ctx = context(
            facts=(fact(participants=("demo-retail",), topic="supplier outage"),),
        )
        empty = InsightEngine(toolkit=FakeToolkit(bank_reports=())).analyze(ctx)
        error = InsightEngine(
            toolkit=FakeToolkit(unavailable=frozenset({"search_bank_reports"}))
        ).analyze(ctx)

        empty_step = next(step for step in empty.trace if step.tool == "search_bank_reports")
        error_step = next(step for step in error.trace if step.tool == "search_bank_reports")
        self.assertEqual(empty_step.status, "empty")
        self.assertEqual(error_step.status, "error")
        self.assertTrue(any(note.startswith("bank_material_not_found:") for note in empty.missing_data))
        self.assertTrue(any(note.startswith("tool_error: search_bank_reports") for note in error.missing_data))

    def test_9_invalid_model_json_falls_back(self):
        class BadJSON:
            def complete(self, messages):
                raise ValueError("not json")

        result = InsightEngine(BadJSON(), llm_retries=0).analyze(context())
        self.assertEqual(result.status, "needs_review")
        self.assertTrue(any("llm_error" in note for note in result.missing_data))

    def test_10_clarification_updates_dependent_claims(self):
        ctx = context(facts=(fact(participants=("demo-retail",), topic="supplier outage"),), bank=True)
        engine = InsightEngine()
        original = engine.analyze(ctx)
        revised = engine.refine(ctx, "Payment was a prepayment, there was no supply dependency")

        self.assertEqual(revised.clarification_applied, "Payment was a prepayment, there was no supply dependency")
        self.assertEqual(revised.status, "needs_review")
        self.assertEqual(revised.priority, original.priority)
        self.assertTrue(
            any("Payment was a prepayment" in claim.limitation for claim in revised.ai_implications)
        )
        self.assertTrue(any(step.action == "clarification" for step in revised.trace))

    def test_11_search_limit_is_enforced(self):
        toolkit = CountingNewsToolkit()
        ctx = context(facts=(fact("A supplier topic is missing metadata."),))
        result = InsightEngine(toolkit=toolkit, max_search_steps=2).analyze(ctx)

        self.assertEqual(toolkit.news_calls, 2)
        self.assertTrue(any(step.action == "search_limit" for step in result.trace))
        self.assertTrue(any(note.startswith("search_limit_reached:") for note in result.missing_data))

    def test_12_missing_bank_material_is_explicit(self):
        ctx = context(facts=(fact(participants=("demo-retail",), topic="supplier outage"),))
        result = InsightEngine(toolkit=FakeToolkit(bank_reports=())).analyze(ctx)

        self.assertTrue(any(note.startswith("bank_material_not_found:") for note in result.missing_data))
        self.assertIn("No bank-specific", result.ai_implications[0].limitation)
        self.assertEqual(result.bank_view["search_status"], "empty")
        self.assertFalse(result.evidence_quality["has_bank_material"])

    def test_13_hypothesis_cannot_be_presented_as_fact(self):
        class FactClaimModel:
            def complete(self, messages):
                return {
                    "event": {"event_id": "e1"},
                    "priority": {},
                    "evidence": [
                        {
                            "evidence_id": "ev-h",
                            "source_id": "demo-doc",
                            "quote": "[DEMO] material",
                            "kind": "hypothesis",
                        }
                    ],
                    "ai_implications": [
                        {
                            "claim_id": "c1",
                            "text": "The supplier will fail.",
                            "evidence_ids": ["ev-h"],
                            "limitation": "",
                            "claim_type": "fact",
                        }
                    ],
                    "status": "completed",
                }

        result = InsightEngine(FactClaimModel(), llm_retries=0).analyze(context())
        self.assertEqual(result.status, "needs_review")
        self.assertTrue(
            any(note.startswith("hypothesis_presented_as_fact:") for note in result.missing_data)
        )

    def test_callable_insight_engine(self):
        result = insight_engine(context(facts=(fact(participants=("demo-retail",), topic="supplier outage"),)))
        self.assertEqual(result.event["event_id"], "e1")
        self.assertIn(result.status, {"completed", "needs_review"})


if __name__ == "__main__":
    unittest.main()
