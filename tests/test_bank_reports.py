"""Тесты распознавания и поиска по аналитике банка — без сети и платных вызовов."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_engine.storage.base import AnalyticsIndex
from data_pipeline.reports import ocr
from data_pipeline.reports.catalog import DOCUMENTS, document_by_id
from data_pipeline.reports.index import BankReportsIndex, search_bank_reports, tokenize

PAGE_TEXT = (
    "**ЦБ РФ принял решение оставить ставку неизменной на уровне 14,0%:** ЦБ РФ взял паузу.\n\n"
    "**Мы ожидаем сохранения паузы на заседании 23 октября:** прогноз ставки на конец 2027 г. — 12,0%.\n\n"
    "[График] Рис. 10: Прогноз ставки\n- Ось X: янв.26, дек.27\n\n"
    "Альфа-Банк, 2026 г. Все права защищены. Генеральная лицензия ЦБ РФ № 1326."
)


def write_page(root: Path, document_id: str, page: int, text: str, confidence: str = "high") -> None:
    folder = root / document_id
    folder.mkdir(parents=True, exist_ok=True)
    record = {"document_id": document_id, "page": page, "text": text, "confidence": confidence,
              "primary": {"text": text}, "verifier": {"text": text}, "prompt_version": ocr.PROMPT_VERSION}
    (folder / f"page_{page:03d}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


class CompareTest(unittest.TestCase):
    def test_identical_and_typographic_variants_agree(self):
        a = "**Ставка** 14,0% — «пауза», ещё"
        b = "Ставка 14,0% - \"пауза\", еще"
        self.assertEqual(ocr.compare_transcriptions(a, b), (1.0, []))

    def test_missing_word_is_reported(self):
        agreement, diffs = ocr.compare_transcriptions(
            "риски связаны с более продолжительным выбытием мощностей",
            "риски связаны с более продолжительным мощностей",
        )
        self.assertLess(agreement, 1.0)
        self.assertEqual(diffs[0]["primary"], "выбытием")

    def test_chart_descriptions_are_ignored(self):
        primary = "Рис. 5: Кредиты\n[График] Рис. 5\n- Ось Y: 1,0%, 2,0%\nИсточники: ЦБ РФ"
        verifier = "Рис. 5: Кредиты\n[График] другое описание\nОсь Y: от 1% до 2%\n(около 0,7% по Y)\nИсточники: ЦБ РФ"
        self.assertEqual(ocr.compare_transcriptions(primary, verifier)[0], 1.0)

    def test_extra_verifier_text_does_not_lower_primary_confidence(self):
        agreement, diffs = ocr.compare_transcriptions("ставка 14%", "ставка 14% лишняя оценка точки")
        self.assertEqual(agreement, 1.0)
        self.assertTrue(diffs)  # но расхождение видно

    def test_confidence_levels(self):
        self.assertEqual(ocr.confidence_level(0.99), "high")
        self.assertEqual(ocr.confidence_level(0.95), "medium")
        self.assertEqual(ocr.confidence_level(0.5), "low")
        self.assertEqual(ocr.confidence_level(None), "unverified")


class OcrPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        import pymupdf

        self.pdf = self.root / "report.pdf"
        with pymupdf.open() as doc:
            doc.new_page().insert_text((72, 72), "test")
            doc.save(self.pdf)
        self.doc = DOCUMENTS[0]
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, verifier_text="ставка 14,0%", fail_primary=False):
        def transcribe(model, png):
            self.calls.append(model)
            self.assertTrue(png.startswith(b"\x89PNG"))
            if model == ocr.PRIMARY_MODEL:
                if fail_primary:
                    raise RuntimeError("timeout")
                return "ставка 14,0%", 0.2
            return verifier_text, 0.3
        return transcribe

    def test_page_saved_with_both_models_and_cached(self):
        out_dir = self.root / "ocr"
        result = ocr.ocr_page(self.doc, self.pdf, 1, 1, out_dir, self.fake())
        self.assertEqual((result.status, result.confidence), ("ok", "high"))
        self.assertAlmostEqual(result.cost, 0.5)
        saved = json.loads(ocr.page_path(out_dir, self.doc.document_id, 1).read_text(encoding="utf-8"))
        self.assertEqual(saved["text"], "ставка 14,0%")
        self.assertEqual(saved["report_date"], self.doc.report_date)
        self.assertEqual({saved["primary"]["model"], saved["verifier"]["model"]},
                         {ocr.PRIMARY_MODEL, ocr.VERIFIER_MODEL})
        again = ocr.ocr_page(self.doc, self.pdf, 1, 1, out_dir, self.fake())
        self.assertEqual(again.status, "cached")
        self.assertEqual(len(self.calls), 2)  # повторный запуск не тратит токены

    def test_primary_failure_is_error_and_nothing_saved(self):
        out_dir = self.root / "ocr"
        result = ocr.ocr_page(self.doc, self.pdf, 1, 1, out_dir, self.fake(fail_primary=True))
        self.assertEqual(result.status, "error")
        self.assertFalse(ocr.page_path(out_dir, self.doc.document_id, 1).exists())

    def test_disagreement_lowers_confidence(self):
        result = ocr.ocr_page(self.doc, self.pdf, 1, 1, self.root / "ocr", self.fake(verifier_text="что-то иное"))
        self.assertEqual(result.confidence, "low")


class IndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        write_page(root, "alfa_policy_rate_2026_09_11", 1, PAGE_TEXT)
        write_page(root, "alfa_russian_steels_1q21", 1, "Цены на сталь растут, экспорт металлургов увеличился.")
        write_page(root, "alfa_gold_digest_2026_08", 2, "Золото подорожало на фоне спроса центробанков.", "low")
        cls.index = BankReportsIndex(root, as_of=date(2026, 10, 22))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_protocol(self):
        self.assertIsInstance(self.index, AnalyticsIndex)

    def test_stemming(self):
        self.assertEqual(tokenize("ключевой ставки")[1], tokenize("ставка")[0])

    def test_search_returns_page_and_report_date(self):
        snippets = self.index.search("прогноз ключевой ставки", limit=3)
        top = snippets[0]
        self.assertEqual(top.metadata["document_id"], "alfa_policy_rate_2026_09_11")
        self.assertEqual(top.metadata["page"], 1)
        self.assertEqual(top.metadata["report_date"], "2026-09-11")
        self.assertIn("стр. 1", top.source)
        self.assertIn("23 октября", top.text)

    def test_boilerplate_not_indexed(self):
        self.assertFalse(any("Все права защищены" in c.text for c in self.index.chunks))

    def test_stale_report_flagged(self):
        snippet = self.index.search("сталь экспорт", limit=1)[0]
        self.assertTrue(snippet.metadata["stale"])
        self.assertTrue(snippet.metadata["notes"])

    def test_newest_report_first_and_warning(self):
        root = Path(self.tmp.name) / "dates"
        write_page(root, "alfa_macro_1h26", 3, "Прогноз ключевой ставки на конец года 13,5%. " * 3)
        write_page(root, "alfa_policy_rate_2026_09_11", 1, "Прогноз ключевой ставки: мы ожидаем сохранения 14,0% до конца года.")
        index = BankReportsIndex(root, as_of=date(2026, 10, 22))
        result = search_bank_reports(index, "прогноз ключевой ставки на конец года")
        self.assertEqual([s["metadata"]["report_date"] for s in result["data"]], ["2026-09-11", "2026-07-31"])
        self.assertTrue(any("разных дат" in w for w in result["warnings"]))

    def test_tool_statuses(self):
        self.assertEqual(search_bank_reports(self.index, "ставка")["status"], "success")
        self.assertEqual(search_bank_reports(self.index, "криптовалюта биткоин")["status"], "empty")
        gold = search_bank_reports(self.index, "золото")
        self.assertTrue(any("низкой уверенностью" in w for w in gold["warnings"]))
        empty_index = BankReportsIndex(Path(self.tmp.name) / "nothing")
        self.assertEqual(search_bank_reports(empty_index, "ставка")["status"], "error")


class CatalogTest(unittest.TestCase):
    def test_ids_unique_and_dates_iso(self):
        ids = [d.document_id for d in DOCUMENTS]
        self.assertEqual(len(ids), len(set(ids)))
        for d in DOCUMENTS:
            if d.report_date:
                date.fromisoformat(d.report_date)
        self.assertIsNotNone(document_by_id("alfa_policy_rate_2026_09_11"))


if __name__ == "__main__":
    unittest.main()
