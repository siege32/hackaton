from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from sxsvn_intel.models import (
    AnalysisStatus,
    CompanyNewsAssessment,
    CompanyProfile,
    EvidenceItem,
    InsightResult,
    NewsFact,
    NewsProcessingResult,
    ProcessingStatus,
    Refinement,
    RelevanceClass,
)
from sxsvn_intel.storage.sql import (
    SqlCompanyRepository,
    SqlNewsRepository,
    SqlStore,
)


def make_fact(fact_id="fact_1", version="v1") -> NewsFact:
    return NewsFact(
        fact_id=fact_id,
        news_id="news-1",
        content_version=version,
        source_id="source-1",
        fact_type="price_change",
        subject="цена на газ",
        action="выросла",
        quote="Цена на газ выросла",
    )


def make_processing(version="v1", fact_id="fact_1") -> NewsProcessingResult:
    return NewsProcessingResult(
        news_id="news-1",
        content_version=version,
        status=ProcessingStatus.PROCESSED,
        general_relevance=RelevanceClass.KEEP,
        classification_reason="business event",
        facts=[make_fact(fact_id, version)],
        model_id="test-model",
        prompt_version="news-facts.v1",
        processing_version="v1",
    )


class SQLiteStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "intel.db")
        self.store = SqlStore.sqlite(self.path)

    def tearDown(self):
        self.store.connection.close()
        self.tmp.cleanup()

    def test_schema_is_idempotent(self):
        self.store.ensure_schema()
        self.store.ensure_schema()
        cursor = self.store.connection.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        self.assertIn("intel_news_processing", tables)
        self.assertIn("intel_news_facts", tables)
        self.assertIn("intel_company_assessments", tables)
        self.assertIn("intel_insight_analyses", tables)

    def test_processing_and_facts_roundtrip_and_version_replacement(self):
        self.store.save_processing_result(make_processing())
        loaded = self.store.get_processing_result("news-1")
        self.assertEqual(loaded.status, ProcessingStatus.PROCESSED)
        self.assertEqual([f.fact_id for f in self.store.get_facts("news-1")], ["fact_1"])

        self.store.save_processing_result(make_processing(version="v2", fact_id="fact_2"))
        self.assertEqual([f.fact_id for f in self.store.get_facts("news-1")], ["fact_2"])
        self.assertEqual(self.store.get_processing_result("news-1").content_version, "v2")
        self.assertEqual(self.store.get_facts("news-1", "v1"), [])

    def test_assessment_history_and_isolation(self):
        assessment = CompanyNewsAssessment(
            news_id="news-1",
            company_id="company-1",
            relevance_class=RelevanceClass.KEEP,
            impact_direction="negative",
            profile_version="pv1",
            content_version="v1",
            evidence=[EvidenceItem("fact_1", "source-1", "Цена на газ выросла", "cost pressure")],
            classification_source="rules",
        )
        self.store.save_assessment(assessment)
        loaded = self.store.get_assessment("news-1", "company-1")
        self.assertEqual(loaded.relevance_class, RelevanceClass.KEEP)
        self.assertIsNone(self.store.get_assessment("news-1", "company-2"))

        assessment.relevance_class = RelevanceClass.REVIEW
        self.store.save_assessment(assessment)
        self.assertEqual(
            self.store.get_assessment("news-1", "company-1").relevance_class,
            RelevanceClass.REVIEW,
        )
        cursor = self.store.connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM intel_company_assessment_history")
        self.assertEqual(cursor.fetchone()[0], 2)

    def test_analysis_and_refinement_roundtrip(self):
        result = InsightResult(
            company_id="company-1",
            analysis_status=AnalysisStatus.PARTIAL,
            summary="partial",
            events=[],
            sources_used=[{"type": "company_profile", "id": "company-1"}],
            additional_searches_used=2,
            analysis_id="analysis-1",
        )
        self.store.save_analysis(result)
        loaded = self.store.get_analysis("analysis-1")
        self.assertEqual(loaded.analysis_status, AnalysisStatus.PARTIAL)
        self.assertEqual(loaded.additional_searches_used, 2)

        refinement = Refinement(
            refinement_id="ref-1",
            company_id="company-1",
            analysis_id="analysis-1",
            text="not relevant",
        )
        self.store.save_refinement(refinement)
        self.assertEqual(len(self.store.list_refinements("analysis-1")), 1)


class SQLAdapterTests(unittest.TestCase):
    def test_news_and_company_adapters_read_existing_schema(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE news (
                id TEXT PRIMARY KEY, title TEXT, text TEXT, source_id TEXT,
                content_version TEXT, published_at TEXT, url TEXT, language TEXT
            );
            CREATE TABLE companies (
                id TEXT PRIMARY KEY, name TEXT, industry TEXT, subindustry TEXT,
                activities TEXT, products_services TEXT, processes TEXT,
                key_resources TEXT, cost_drivers TEXT, geography TEXT,
                suppliers TEXT, customers TEXT, financials TEXT,
                known_risks TEXT, data_sources TEXT, updated_at TEXT,
                profile_version TEXT
            );
            INSERT INTO news VALUES ('n1','t','body','s1','v1','2026-01-01',NULL,'ru');
            INSERT INTO companies VALUES
                ('c1','Gas Co','Нефтегаз',NULL,'["добыча газа"]','["газ"]',NULL,
                 NULL,'["энергия"]','["Россия"]','[]','[]','{"revenue":100}',
                 '[]','[]','2026-01-01',NULL);
            """
        )
        news = SqlNewsRepository(connection).get_news("n1")
        self.assertEqual(news.news_id, "n1")
        self.assertEqual(news.content_version, "v1")

        company = SqlCompanyRepository(connection).get_company("c1")
        self.assertEqual(company.company_id, "c1")
        self.assertIn("добыча газа", company.activities)
        self.assertEqual(company.financials["revenue"], 100)
        self.assertTrue(company.profile_version)
        connection.close()


if __name__ == "__main__":
    unittest.main()
