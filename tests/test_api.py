import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ai_engine.config import Settings
from ai_engine.models import AnalysisResult
from api import main
from api.schemas import ParsedNewsResponse


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.app.state.news_parser = None
        self.settings_patch = patch.object(main, "get_settings", return_value=Settings(_env_file=None))
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    @staticmethod
    def result():
        return AnalysisResult(
            event={}, priority={}, company_features={}, bank_view={},
            ai_implications=(), risks=(), opportunities=(), missing_data=(),
            next_check=None, evidence=(), trace_id="trace-test", status="needs_review",
        )

    def test_health(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_analyze_returns_engine_contract(self):
        payload = {
            "company_id": "company-123",
            "event_id": "event-456",
            "as_of_date": "2026-10-21",
            "profile": {"company_id": "company-123", "name": "Example Co"},
            "event_facts": [
                {"fact_id": "fact-1", "text": "A supplier reported a production outage.", "source_ids": ["source-1"]}
            ],
            "sources": [{"source_id": "source-1", "title": "News", "kind": "news"}],
        }

        response = self.client.post("/analyze", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertIn(response.json()["status"], {"completed", "needs_review", "failed"})
        self.assertIn("evidence", response.json())
        self.assertIn("trace_id", response.json())

    def test_analyze_rejects_missing_required_fields(self):
        response = self.client.post("/analyze", json={"company_id": "company-123"})

        self.assertEqual(response.status_code, 422)

    def test_news_analysis_uses_configured_llm_and_builds_engine_context(self):
        payload = {
            "event_id": "event-456",
            "as_of_date": "2026-10-21",
            "profile": {"company_id": "company-123", "name": "Example Co"},
            "news": {
                "news_id": "news-1",
                "text": "Supplier reported a production outage.",
                "source_id": "source-1",
                "title": "Production outage",
            },
        }
        settings = Settings(_env_file=None, SXSVN_LLM_API_KEY="test-key")
        with (
            patch.object(main, "get_settings", return_value=settings),
            patch.object(main, "OpenAICompatibleClient") as client_factory,
            patch.object(main, "insight_engine", return_value=self.result()) as engine,
        ):
            response = self.client.post("/v1/analyze/news", json=payload)

        self.assertEqual(response.status_code, 200)
        client_factory.assert_called_once_with(settings)
        context = engine.call_args.args[0]
        self.assertEqual(context.company_id, "company-123")
        self.assertEqual(context.news.news_id, "news-1")
        self.assertEqual(context.sources[0].source_id, "source-1")
        self.assertIs(engine.call_args.kwargs["llm"], client_factory.return_value)

    def test_parser_route_reports_unconfigured_service(self):
        response = self.client.post("/v1/news/parse", json={"raw_text": "An article body."})

        self.assertEqual(response.status_code, 503)

    def test_parser_route_uses_configured_adapter(self):
        expected = ParsedNewsResponse.model_validate({
            "news": {
                "news_id": "news-1", "text": "Article body.", "source_id": "source-1",
                "title": "Article", "url": "https://example.com/news/1",
            },
            "source": {
                "source_id": "source-1", "title": "Article",
                "url": "https://example.com/news/1", "kind": "news",
            },
        })

        class FakeParser:
            def parse(self, request):
                self.request = request
                return expected

        parser = FakeParser()
        main.app.state.news_parser = parser
        response = self.client.post("/v1/news/parse", json={"raw_text": "Article body."})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["news"]["news_id"], "news-1")
        self.assertEqual(parser.request.raw_text, "Article body.")

    def test_parser_request_requires_exactly_one_input(self):
        response = self.client.post(
            "/v1/news/parse",
            json={"raw_text": "Article body.", "source_url": "https://example.com/news/1"},
        )

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()