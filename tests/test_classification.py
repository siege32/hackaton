from __future__ import annotations

import unittest

from ai_engine.models import CompanyProfile, ImpactDirection, ImpactType, NewsRecord, RelevanceClass, Relationship
from tests.helpers import (
    COMPANY_PROMPT,
    FACT_PROMPT,
    GAS_NEWS,
    assessment_payload,
    confirmed_supplier,
    energy_intensive,
    fact_payload,
    gas_producer,
    it_company,
    make_service,
    make_settings,
    payment_link,
    sector_only_company,
    sparse_company,
)


class ContextualClassificationTests(unittest.TestCase):
    def test_same_news_has_different_results_and_directions_for_different_companies(self):
        service, store, news_repo, company_repo, *_ = make_service()
        producer = service.classify_news_for_company(GAS_NEWS.news_id, "company-gas-producer")
        cement = service.classify_news_for_company(GAS_NEWS.news_id, "company-cement")

        self.assertEqual(producer.relevance_class, RelevanceClass.KEEP)
        self.assertEqual(producer.impact_direction, ImpactDirection.POSITIVE)
        self.assertEqual(producer.impact_type, ImpactType.INDIRECT)
        self.assertTrue(producer.evidence)

        self.assertEqual(cement.relevance_class, RelevanceClass.KEEP)
        self.assertEqual(cement.impact_direction, ImpactDirection.NEGATIVE)
        self.assertEqual(cement.impact_type, ImpactType.INDIRECT)

        self.assertNotEqual(producer.company_id, cement.company_id)
        self.assertNotEqual(producer.assessment_id, cement.assessment_id)
        self.assertEqual(store.get_assessment(GAS_NEWS.news_id, "company-gas-producer").company_id, "company-gas-producer")
        self.assertEqual(store.get_assessment(GAS_NEWS.news_id, "company-cement").company_id, "company-cement")

    def test_company_name_absence_is_not_automatic_drop(self):
        service, *_ = make_service()
        assessment = service.classify_news_for_company(GAS_NEWS.news_id, "company-gas-producer")
        self.assertNotIn("Газодобывающая компания", GAS_NEWS.text)
        self.assertEqual(assessment.relevance_class, RelevanceClass.KEEP)

    def test_industry_coincidence_alone_is_not_keep(self):
        service, *_ = make_service(companies=[sector_only_company()])
        assessment = service.classify_news_for_company(GAS_NEWS.news_id, "company-sector")
        self.assertEqual(assessment.relevance_class, RelevanceClass.REVIEW)
        self.assertNotEqual(assessment.relevance_class, RelevanceClass.KEEP)

    def test_unrelated_company_is_dropped_but_missing_data_is_review(self):
        service, *_ = make_service()
        it = service.classify_news_for_company(GAS_NEWS.news_id, "company-it")
        self.assertEqual(it.relevance_class, RelevanceClass.DROP)

        sparse_service, *_ = make_service(companies=[sparse_company()])
        sparse = sparse_service.classify_news_for_company(GAS_NEWS.news_id, "company-sparse")
        self.assertEqual(sparse.relevance_class, RelevanceClass.REVIEW)
        self.assertTrue(sparse.missing_data)

    def test_unconfirmed_relationship_does_not_become_a_confirmed_dependency(self):
        relationship = Relationship(
            relationship_id="rel-unconfirmed",
            company_id="company-it",
            counterparty_id="cp-gazprom",
            counterparty_name="Газпром",
            relation_type=__import__("ai_engine.models", fromlist=["RelationType"]).RelationType.SUPPLIER,
            confirmed=False,
            significance=__import__("ai_engine.models", fromlist=["Significance"]).Significance.UNKNOWN,
            evidence=["mention in an article"],
            source="news",
        )
        relation_news = NewsRecord(
            news_id="news-relation",
            title="Газпром и газ",
            text="Газпром сообщил, что цена на природный газ выросла на 20% в январе 2026 года.",
            source_id="source-1",
        )
        service, *_ = make_service(news=[relation_news], relationships=[relationship])
        assessment = service.classify_news_for_company(relation_news.news_id, "company-it")
        self.assertEqual(assessment.relevance_class, RelevanceClass.REVIEW)
        self.assertNotEqual(assessment.impact_type, ImpactType.DIRECT)
        self.assertTrue(any("not confirmed" in limitation for limitation in assessment.limitations))
        self.assertTrue(any("counterparty" in gap.lower() or "dependency" in gap.lower() for gap in assessment.missing_data))

    def test_payment_link_is_not_treated_as_dependency(self):
        relation_news = NewsRecord(
            news_id="news-payment",
            title="Газпром и газ",
            text="Газпром сообщил, что цена на природный газ выросла на 20% в январе 2026 года.",
            source_id="source-1",
        )
        service, *_ = make_service(
            news=[relation_news], relationships=[payment_link("company-it", "Газпром")]
        )
        assessment = service.classify_news_for_company(relation_news.news_id, "company-it")
        self.assertEqual(assessment.relevance_class, RelevanceClass.DROP)
        self.assertEqual(assessment.impact_direction, ImpactDirection.UNKNOWN)

    def test_profile_change_reassesses_without_reextracting_facts(self):
        service, store, news_repo, company_repo, rel_repo, llm = make_service()
        first = service.classify_news_for_company(GAS_NEWS.news_id, "company-gas-producer")
        self.assertEqual(llm.count(FACT_PROMPT), 1)

        changed = gas_producer()
        changed.cost_drivers = ["природный газ", "электроэнергия"]
        changed.profile_version = ""  # recompute
        changed.__post_init__()
        company_repo.add(changed)
        second = service.classify_news_for_company(GAS_NEWS.news_id, "company-gas-producer")
        self.assertEqual(llm.count(FACT_PROMPT), 1, "facts must not be re-extracted")
        self.assertNotEqual(first.profile_version, second.profile_version)
        self.assertEqual(second.profile_version, changed.profile_version)
        self.assertNotEqual(first.assessment_id, second.assessment_id)

    def test_assessment_keeps_profile_and_source_metadata(self):
        service, *_ = make_service()
        assessment = service.classify_news_for_company(GAS_NEWS.news_id, "company-cement")
        self.assertEqual(assessment.content_version, GAS_NEWS.content_version)
        self.assertEqual(assessment.profile_version, energy_intensive().profile_version)
        self.assertEqual(assessment.classification_source, "rules")
        self.assertTrue(assessment.supporting_fact_ids)
        self.assertTrue(all(item.quote for item in assessment.evidence))


class LLMClassificationValidationTests(unittest.TestCase):
    def _service(self):
        settings = make_settings(classifier_mode="llm")
        return make_service(settings=settings)

    def test_valid_grounded_llm_assessment_is_saved(self):
        service, store, *_ = self._service()
        processing = service.process_news(GAS_NEWS.news_id)
        fact_id = processing.facts[0].fact_id
        service.llm.push(
            COMPANY_PROMPT,
            assessment_payload(
                "company-cement",
                fact_ids=[fact_id],
                direction="negative",
                impact_type="indirect",
            ),
        )
        assessment = service.classify_news_for_company(GAS_NEWS.news_id, "company-cement")
        self.assertEqual(assessment.relevance_class, RelevanceClass.KEEP)
        self.assertEqual(assessment.classification_source, "llm")
        self.assertEqual(assessment.evidence[0].fact_id, fact_id)

    def test_invalid_enum_becomes_explicit_review(self):
        service, *_ = self._service()
        processing = service.process_news(GAS_NEWS.news_id)
        service.llm.push(
            COMPANY_PROMPT,
            assessment_payload("company-cement", relevance="MAYBE", fact_ids=[processing.facts[0].fact_id]),
        )
        assessment = service.classify_news_for_company(GAS_NEWS.news_id, "company-cement")
        self.assertEqual(assessment.relevance_class, RelevanceClass.REVIEW)
        self.assertEqual(assessment.classification_source, "llm_rejected")
        self.assertTrue(any("validation" in item for item in assessment.limitations))

    def test_keep_with_fabricated_fact_reference_is_downgraded(self):
        service, *_ = self._service()
        service.process_news(GAS_NEWS.news_id)
        payload = assessment_payload(
            "company-cement",
            fact_ids=["fact_does_not_exist"],
            evidence=[
                {
                    "fact_id": "fact_does_not_exist",
                    "source_id": "x",
                    "quote": "Цена на природный газ выросла на 20%",
                    "supports": "x",
                }
            ],
        )
        service.llm.push(COMPANY_PROMPT, payload)
        assessment = service.classify_news_for_company(GAS_NEWS.news_id, "company-cement")
        self.assertEqual(assessment.relevance_class, RelevanceClass.REVIEW)
        self.assertEqual(assessment.supporting_fact_ids, [])
        self.assertTrue(any("without verifiable fact references" in item for item in assessment.limitations))

    def test_fabricated_quote_is_dropped(self):
        service, *_ = self._service()
        processing = service.process_news(GAS_NEWS.news_id)
        fact_id = processing.facts[0].fact_id
        payload = assessment_payload(
            "company-cement",
            fact_ids=[fact_id],
            evidence=[
                {
                    "fact_id": fact_id,
                    "source_id": "x",
                    "quote": "Совершенно другая цитата, которой нет в новости",
                    "supports": "x",
                }
            ],
        )
        service.llm.push(COMPANY_PROMPT, payload)
        assessment = service.classify_news_for_company(GAS_NEWS.news_id, "company-cement")
        self.assertEqual(assessment.relevance_class, RelevanceClass.REVIEW)
        self.assertEqual(assessment.evidence, [])

    def test_wrong_company_id_is_rejected(self):
        service, *_ = self._service()
        processing = service.process_news(GAS_NEWS.news_id)
        payload = assessment_payload(
            "company-gas-producer", fact_ids=[processing.facts[0].fact_id]
        )
        service.llm.push(COMPANY_PROMPT, payload)
        assessment = service.classify_news_for_company(GAS_NEWS.news_id, "company-cement")
        self.assertEqual(assessment.relevance_class, RelevanceClass.REVIEW)
        self.assertEqual(assessment.classification_source, "llm_rejected")

    def test_llm_results_for_two_companies_stay_independent(self):
        service, store, *_ = self._service()
        processing = service.process_news(GAS_NEWS.news_id)
        fact_id = processing.facts[0].fact_id
        service.llm.push(
            COMPANY_PROMPT,
            assessment_payload("company-gas-producer", fact_ids=[fact_id], direction="positive"),
        )
        service.llm.push(
            COMPANY_PROMPT,
            assessment_payload("company-cement", fact_ids=[fact_id], direction="negative"),
        )
        producer = service.classify_news_for_company(GAS_NEWS.news_id, "company-gas-producer")
        cement = service.classify_news_for_company(GAS_NEWS.news_id, "company-cement")
        self.assertEqual(producer.impact_direction, ImpactDirection.POSITIVE)
        self.assertEqual(cement.impact_direction, ImpactDirection.NEGATIVE)
        self.assertEqual(store.get_assessment(GAS_NEWS.news_id, "company-gas-producer").impact_direction, ImpactDirection.POSITIVE)
        self.assertEqual(store.get_assessment(GAS_NEWS.news_id, "company-cement").impact_direction, ImpactDirection.NEGATIVE)


if __name__ == "__main__":
    unittest.main()
