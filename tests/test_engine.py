import unittest

from ai_engine.agent import InsightEngine as ProductionInsightEngine
from ai_engine.config import Settings
from ai_engine.llm import ALLOWED_MODELS, CHANGELLENGE_BASE_URL, DEFAULT_MODEL, LLMError, OpenAICompatibleClient
from ai_engine.models import AnalysisContext, BankEvidence, CompanyProfile, EventFact, Evidence
from ai_engine.validation import validate_result
from tests.fakes import FakeToolkit


class InsightEngine(ProductionInsightEngine):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("toolkit", FakeToolkit())
        super().__init__(*args, **kwargs)


def context(*, bank=False, company="acme", event="e1"):
    return AnalysisContext(
        company_id=company, event_id=event, as_of_date="2026-10-21",
        profile=CompanyProfile(company_id=company, name=company.title(), industries=("retail",), features={"size": "medium"}),
        event_facts=(EventFact(fact_id="f1", text="Supplier announced a production outage.", source_ids=("s1",)),),
        bank_evidence=({"source_id": "b1", "text": "Observed payments to supplier."},) if bank else (),
    )


class InsightEngineTests(unittest.TestCase):
    def test_missing_toolkit_fails_closed_without_synthetic_data(self):
        result = ProductionInsightEngine().analyze(context(bank=True))
        self.assertEqual(result.status, "needs_review")
        self.assertTrue(any(note.startswith("tool_error:") for note in result.missing_data))
        self.assertNotIn("demo", result.bank_view)

    def test_direct_event(self):
        result = InsightEngine().analyze(context(bank=True))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.event["event_id"], "e1")

    def test_missing_bank_evidence_is_explicit(self):
        result = InsightEngine().analyze(context())
        self.assertEqual(result.status, "needs_review")
        self.assertIsNotNone(result.next_check)

    def test_indirect_or_unrelated_event_does_not_invent_link(self):
        result = InsightEngine().analyze(context(company="other"))
        self.assertIn("No bank-specific", result.ai_implications[0].limitation)

    def test_cache_avoids_repeat_processing(self):
        engine = InsightEngine()
        self.assertIs(engine.analyze(context()), engine.analyze(context()))

    def test_context_ids_are_consistent(self):
        bad = context()
        bad = bad.model_copy(update={"profile": CompanyProfile(company_id="different", name="Different")})
        with self.assertRaises(ValueError):
            InsightEngine().analyze(bad)

    def test_event_without_source_is_not_fabricated_as_evidence(self):
        base = context()
        no_source = base.model_copy(update={"event_facts": (EventFact(fact_id="f1", text="Unknown", source_ids=()),)})
        result = InsightEngine().analyze(no_source)
        self.assertEqual(result.evidence, ())
        self.assertEqual(result.status, "needs_review")

    def test_empty_event_has_zero_priority(self):
        base = context()
        empty = base.model_copy(update={"event_facts": ()})
        self.assertEqual(InsightEngine().analyze(empty).priority["priority_score"], 0.0)

    def test_trace_id_is_returned(self):
        self.assertTrue(InsightEngine().analyze(context()).trace_id)

    def test_run_id_is_preserved(self):
        base = context()
        run = base.model_copy(update={"run_id": "run-123"})
        self.assertEqual(InsightEngine().analyze(run).trace_id, "run-123")

    def test_unconfigured_adapter_fails_clearly(self):
        with self.assertRaises(LLMError):
            OpenAICompatibleClient(Settings(_env_file=None)).complete([])

    def test_changellenge_defaults_are_exact(self):
        config = Settings(_env_file=None)
        self.assertEqual(config.llm_base_url, CHANGELLENGE_BASE_URL)
        self.assertEqual(config.llm_model, DEFAULT_MODEL)
        self.assertIn(config.llm_model, ALLOWED_MODELS)

    def test_unknown_model_is_rejected_before_network_call(self):
        with self.assertRaises(LLMError):
            OpenAICompatibleClient(Settings(_env_file=None, llm_api_key="sk-test", llm_model="not-approved")).complete([])

    def test_model_bad_json_falls_back_to_review(self):
        class BadModel:
            def complete(self, messages):
                raise ValueError("bad JSON")

        result = InsightEngine(BadModel()).analyze(context())
        self.assertEqual(result.status, "needs_review")
        self.assertTrue(any("llm_error" in value for value in result.missing_data))

    def test_model_claim_without_evidence_is_reviewed(self):
        class Model:
            def complete(self, messages):
                return {"event": {"event_id": "e1"}, "priority": {}, "ai_implications": [{"claim_id": "c", "text": "x", "evidence_ids": []}], "status": "completed"}

        result = InsightEngine(Model()).analyze(context())
        self.assertEqual(result.status, "needs_review")

    def test_validation_accepts_event_evidence(self):
        result = InsightEngine().analyze(context())
        self.assertEqual(validate_result(result, context()), [])

    def test_bank_evidence_is_typed_and_forward_compatible(self):
        base = context()
        enriched = AnalysisContext.model_validate(
            {**base.to_dict(), "bank_evidence": [{"source_id": "b1", "text": "Payments observed.", "amount": 42}]}
        )
        self.assertIsInstance(enriched.bank_evidence[0], BankEvidence)
        self.assertEqual(enriched.bank_evidence[0].amount, 42)

    def test_bank_evidence_source_is_a_valid_citation(self):
        base = context(bank=True)
        result = InsightEngine().analyze(base)
        result = result.model_copy(
            update={"evidence": (*result.evidence, Evidence(evidence_id="ev-b", source_id="b1", quote="Payments observed.", kind="bank"))}
        )
        self.assertEqual(validate_result(result, base), [])

    def test_duplicate_evidence_and_claim_ids_are_flagged(self):
        base = context()
        result = InsightEngine().analyze(base)
        duplicated = result.model_copy(
            update={"evidence": (*result.evidence, result.evidence[0]), "ai_implications": (*result.ai_implications, result.ai_implications[0])}
        )
        codes = {issue.code for issue in validate_result(duplicated, base)}
        self.assertIn("duplicate_evidence_id", codes)
        self.assertIn("duplicate_claim_id", codes)

    def test_retry_recovers_from_invalid_model_payload(self):
        calls = {"count": 0}

        class FlakyModel:
            def complete(self, messages):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {
                        "event": {"event_id": "e1"}, "priority": {},
                        "ai_implications": [{"claim_id": "c", "text": "x", "evidence_ids": [], "injected": "field"}],
                    }
                return {
                    "event": {"event_id": "e1"}, "priority": {"priority_score": 0.2},
                    "company_features": {}, "bank_view": {},
                    "ai_implications": [], "risks": [], "opportunities": [],
                    "missing_data": [], "next_check": None,
                    "evidence": [{"evidence_id": "ev-1", "source_id": "s1", "quote": "q", "kind": "event"}],
                    "status": "completed",
                }

        result = InsightEngine(FlakyModel()).analyze(context(bank=True))
        self.assertEqual(result.status, "completed")
        self.assertEqual(calls["count"], 2)

    def test_invalid_model_payload_falls_back_instead_of_raising(self):
        calls = {"count": 0}

        class ExtraFieldModel:
            def complete(self, messages):
                calls["count"] += 1
                return {
                    "event": {"event_id": "e1"}, "priority": {},
                    "evidence": [],
                    "ai_implications": [{"claim_id": "c", "text": "x", "evidence_ids": [], "injected": "field"}],
                }

        result = InsightEngine(ExtraFieldModel(), llm_retries=1).analyze(context())
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(calls["count"], 2)
        self.assertTrue(any("llm_error" in value for value in result.missing_data))

    def test_negative_search_steps_are_rejected(self):
        with self.assertRaises(ValueError):
            InsightEngine(max_search_steps=-1)


if __name__ == "__main__":
    unittest.main()
