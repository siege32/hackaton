from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app
from tests.test_data_pipeline import COMPANIES, PAYMENTS, PERSONS
from data_pipeline.store import build_database


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        raw = self.root / "raw"
        raw.mkdir()
        (raw / "df_Uid_data_hack.csv").write_text(COMPANIES, encoding="utf-8")
        (raw / "df_Uid_payment_hack.csv").write_text(PAYMENTS, encoding="utf-8")
        (raw / "df_Fid_Uid_link_hack.csv").write_text(PERSONS, encoding="utf-8")
        self.company_db = self.root / "companies.sqlite"
        build_database(raw, self.company_db)
        self.env = patch.dict(os.environ, {
            "DATA_DB_PATH": str(self.company_db),
            "NEWS_DB_PATH": str(self.root / "news.sqlite"),
            "BANK_REPORTS_OCR_DIR": str(Path(__file__).parents[1] / "data" / "bank_reports" / "ocr"),
            "INTEL_SQLITE_PATH": str(self.root / "intel.sqlite"),
            "API_JOBS_DB_PATH": str(self.root / "jobs.sqlite"),
            "CLASSIFIER_MODE": "rules",
            "LLM_API_KEY": "",
        })
        self.env.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_health_reports_real_service_availability(self):
        response = self.client.get("/api/v1/health")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["services"]["company_data"])
        self.assertTrue(response.json()["services"]["bank_reports"])

    def test_root_serves_web_application(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("SXSVN", response.text)

    def test_company_profile_and_exposure_use_data_pipeline(self):
        profile = self.client.get("/api/v1/companies/U_1")
        exposure = self.client.get("/api/v1/companies/U_1/exposure?okved_filter=B")

        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["company_id"], "U_1")
        self.assertEqual(exposure.status_code, 200)
        self.assertAlmostEqual(exposure.json()["incoming"]["value"], 0.6)
        self.assertAlmostEqual(exposure.json()["incoming"]["value_upper_bound"], 0.7)

    def test_news_catalog_accepts_and_searches_manual_news(self):
        created = self.client.post("/api/v1/news", json={
            "title": "Новые пошлины на удобрения",
            "text": "Правительство ввело экспортные пошлины на минеральные удобрения.",
            "published_at": "2026-10-22",
        })
        found = self.client.get("/api/v1/news/search", params={"query": "пошлины удобрения"})

        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["status"], "new")
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["data"][0]["news_id"], created.json()["news_id"])

        # Фильтр релевантности из интерфейса доходит до поиска (раньше параметр игнорировался).
        prefilter = created.json()["prefilter"]
        other = [value for value in ("KEEP", "REVIEW", "DROP") if value != prefilter]
        only_other = self.client.get("/api/v1/news/search", params={"query": "пошлины удобрения", "prefilter": other})
        self.assertEqual(only_other.json()["status"], "empty")
        self.assertEqual(self.client.get("/api/v1/news/search", params={"query": "пошлины", "prefilter": "X"}).status_code, 422)

    def test_report_search_reads_prepared_ocr_index(self):
        reports = self.client.get("/api/v1/reports")
        snippets = self.client.get("/api/v1/reports/search", params={"query": "ключевая ставка"})

        self.assertEqual(reports.status_code, 200)
        self.assertEqual(len(reports.json()["items"]), 9)
        self.assertEqual(snippets.status_code, 200)
        self.assertTrue(snippets.json()["items"])
        self.assertIn("page", snippets.json()["items"][0]["metadata"])

    def test_analysis_returns_job_and_marks_missing_data_job_failed(self):
        created = self.client.post("/api/v1/news", json={
            "title": "Событие для анализа",
            "text": "Компания сообщает о росте стоимости ключевых ресурсов.",
        })
        queued = self.client.post("/api/v1/analysis", json={
            "company_id": "U_1", "event_ids": [created.json()["news_id"]],
        })
        status = self.client.get(queued.json()["status_url"])

        self.assertEqual(queued.status_code, 200)
        self.assertEqual(queued.json()["status"], "queued")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], "failed")
        self.assertIn("LLM", status.json()["error"])

    def test_analysis_rejects_unknown_company_before_creating_a_job(self):
        response = self.client.post("/api/v1/analysis", json={
            "company_id": "missing-company", "event_ids": ["missing-news"],
        })

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Компания не найдена")

    def test_analysis_rejects_unknown_news_before_creating_a_job(self):
        response = self.client.post("/api/v1/analysis", json={
            "company_id": "U_1", "event_ids": ["missing-news"],
        })

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["news_ids"], ["missing-news"])

    def test_refinement_rejects_unknown_analysis(self):
        response = self.client.post("/api/v1/analysis/missing-analysis/refinements", json={
            "text": "Проверочный вопрос к отсутствующему разбору",
        })

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Разбор не найден")


if __name__ == "__main__":
    unittest.main()
