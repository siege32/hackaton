from __future__ import annotations

import unittest

from ai_engine.errors import GroundingValidationError
from ai_engine.models import NewsRecord, ProcessingStatus, RelevanceClass
from tests.helpers import FACT_PROMPT, GAS_NEWS, fact_payload, make_service


class FactExtractionTests(unittest.TestCase):
    def test_facts_are_extracted_validated_and_saved(self):
        service, store, *_ = make_service()
        result = service.process_news(GAS_NEWS.news_id)
        self.assertEqual(result.status, ProcessingStatus.PROCESSED)
        self.assertEqual(result.general_relevance, RelevanceClass.KEEP)
        self.assertEqual(len(result.facts), 1)
        fact = result.facts[0]
        self.assertTrue(fact.fact_id.startswith("fact_"))
        self.assertEqual(fact.date, "январь 2026")
        self.assertEqual(fact.quantities[0].raw, "20%")
        self.assertEqual(fact.quantities[0].value, 20.0)
        self.assertEqual(fact.quantities[0].unit, "%")
        self.assertEqual(fact.origin.value, "source")
        self.assertEqual(len(store.get_facts(GAS_NEWS.news_id)), 1)

    def test_ungrounded_model_facts_are_rejected(self):
        payload = fact_payload(quote="Этой цитаты нет в тексте")
        service, store, *_ = make_service(responses={FACT_PROMPT: [payload]})
        with self.assertRaises(GroundingValidationError):
            service.process_news(GAS_NEWS.news_id)
        self.assertEqual(store.get_facts(GAS_NEWS.news_id), [])

    def test_numbers_are_taken_from_source_raw_not_from_model_value(self):
        payload = fact_payload()
        payload["facts"][0]["quantities"] = [{"value": 999, "unit": "%", "raw": "20%"}]
        service, store, *_ = make_service(responses={FACT_PROMPT: [payload]})
        result = service.process_news(GAS_NEWS.news_id)
        self.assertEqual(result.facts[0].quantities[0].value, 20.0)
        self.assertTrue(any("disagreed with source" in w for w in result.validation_warnings))

    def test_quantity_not_present_in_quote_is_dropped(self):
        payload = fact_payload()
        payload["facts"][0]["quantities"] = [{"value": 30, "unit": "%", "raw": "30%"}]
        service, store, *_ = make_service(responses={FACT_PROMPT: [payload]})
        result = service.process_news(GAS_NEWS.news_id)
        self.assertEqual(result.facts[0].quantities, [])
        self.assertTrue(any("not part of its quote" in w for w in result.validation_warnings))

    def test_review_is_a_business_class_not_a_technical_error(self):
        service, store, *_ = make_service(responses={FACT_PROMPT: [fact_payload("REVIEW")]})
        result = service.process_news(GAS_NEWS.news_id)
        self.assertEqual(result.status, ProcessingStatus.PROCESSED)
        self.assertEqual(result.general_relevance, RelevanceClass.REVIEW)

    def test_drop_is_persisted_with_reason(self):
        payload = fact_payload("DROP")
        payload["classification_reason"] = "Celebrity gossip, not a business event."
        service, store, *_ = make_service(responses={FACT_PROMPT: [payload]})
        result = service.process_news(GAS_NEWS.news_id)
        self.assertEqual(result.status, ProcessingStatus.PROCESSED)
        self.assertEqual(result.general_relevance, RelevanceClass.DROP)
        self.assertIn("gossip", result.classification_reason)

    def test_processing_is_idempotent_and_does_not_duplicate_facts(self):
        llm_calls = []
        service, store, _, _, _, llm = make_service()
        first = service.process_news(GAS_NEWS.news_id)
        second = service.process_news(GAS_NEWS.news_id)
        self.assertEqual(first.facts[0].fact_id, second.facts[0].fact_id)
        self.assertEqual(llm.count(FACT_PROMPT), 1)
        forced = service.process_news(GAS_NEWS.news_id, force=True)
        self.assertEqual(len(store.get_facts(GAS_NEWS.news_id)), 1)
        self.assertEqual(forced.facts[0].fact_id, first.facts[0].fact_id)

    def test_new_content_version_does_not_inherit_old_facts(self):
        service, store, news_repo, *_ = make_service()
        first = service.process_news(GAS_NEWS.news_id)
        changed = NewsRecord(
            news_id=GAS_NEWS.news_id,
            title="Другая новость",
            text="Цена на природный газ снизилась на 5% в феврале 2026 года.",
            source_id="source-1",
        )
        news_repo.add(changed)
        payload = fact_payload(quote="Цена на природный газ снизилась на 5%", action="снизилась")
        payload["facts"][0]["quantities"] = [{"value": 5, "unit": "%", "raw": "5%"}]
        service.llm._responses[FACT_PROMPT] = [payload]
        second = service.process_news(GAS_NEWS.news_id, force=True)
        self.assertNotEqual(first.content_version, second.content_version)
        facts = store.get_facts(GAS_NEWS.news_id)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].content_version, changed.content_version)
        self.assertNotEqual(facts[0].fact_id, first.facts[0].fact_id)

    def test_processing_version_change_triggers_reprocessing(self):
        service, store, _, _, _, llm = make_service()
        service.process_news(GAS_NEWS.news_id)
        service.settings = service.settings.with_overrides(processing_version="v2")
        service.extractor.settings = service.settings
        service.process_news(GAS_NEWS.news_id)
        self.assertEqual(llm.count(FACT_PROMPT), 2)

    def test_empty_text_is_stored_as_failed_and_not_retried_forever(self):
        empty = NewsRecord(news_id="news-empty", title="", text="", source_id="s")
        service, store, news_repo, *_ = make_service(news=[empty])
        result = service.process_news("news-empty")
        self.assertEqual(result.status, ProcessingStatus.FAILED)
        self.assertEqual(result.error_code, "news_text_empty")
        self.assertEqual(store.get_processing_result("news-empty").error_code, "news_text_empty")


if __name__ == "__main__":
    unittest.main()
