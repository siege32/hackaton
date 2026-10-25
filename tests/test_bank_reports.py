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
from data_pipeline.reports.index import BankReportsIndex, _split_page, clean_text, search_bank_reports, tokenize

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


CHART_PAGE = """**А**

# Центр макроэкономического анализа

**Рис. 10: Прогноз Альфа-Банка по движению ключевой ставки, %**

[График] Рис. 10: Прогноз Альфа-Банка по движению ключевой ставки, %
- Ось Y: 17,0; 16,0; 15,0; 14,0; 13,0; 12,0
- Ось X: окт.26, дек.26, дек.27
- Подписи значений: 14,0%, 14,0%, 12,0%

*Источники: ЦБ РФ, Альфа-Банк*

**Рис. 11: Динамика курса рубля, руб./$**

[График] Рис. 11: Динамика курса рубля, руб./$
- Ось Y: 115, 105, 95, 85, 75, 65
- Ось X: авг.23, ноя.23, фев.24, май.24, авг.24
- Легенда: USD/RUB

□ **Курс рубля останется крепким.** Мы ожидаем, что курс рубля к доллару до конца года останется в диапазоне 80–85 руб./$ на фоне высоких ставок и слабого импорта, а *риски ослабления* связаны с бюджетом.

| Показатель | 2026 | 2027 |
|---|---|---|
| Курс рубля на конец года, руб./$ | 82 | 88 |
| Инфляция на конец года, % г/г | 7,5 | 5,0 |

Аналитика Альфа-КИБ | Обзор. Август 2026 г. — 3
"""


class CleanupTest(unittest.TestCase):
    """Чистка OCR-текста: разметка, мусорные символы, шкалы графиков, виды фрагментов."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        write_page(Path(self.tmp.name), "alfa_policy_rate_2026_09_11", 3, CHART_PAGE)
        self.index = BankReportsIndex(Path(self.tmp.name), as_of=date(2026, 10, 22))

    def tearDown(self):
        self.tmp.cleanup()

    def test_markup_and_artifacts_removed(self):
        text = clean_text(CHART_PAGE)
        for junk in ("**", "□", "# ", "Источники", "Аналитика Альфа-КИБ", "Центр макроэкономического"):
            self.assertNotIn(junk, text)
        self.assertNotIn("А", text.splitlines())  # логотип банка, распознанный как буква
        self.assertIn("• Курс рубля останется крепким.", text)
        self.assertIn("а риски ослабления связаны", text)

    def test_chart_axes_dropped_but_labelled_dates_kept(self):
        kinds = dict((text.splitlines()[0], kind) for kind, text in _split_page(CHART_PAGE))
        charts = [text for kind, text in _split_page(CHART_PAGE) if kind == "chart"]
        self.assertEqual(len(charts), 2)
        forecast, rouble = charts
        self.assertNotIn("Ось Y", forecast + rouble)
        self.assertIn("Ось X: окт.26, дек.26, дек.27", forecast)  # даты к подписанным значениям
        self.assertNotIn("Ось X", rouble)  # без подписанных значений ось X — просто шкала
        self.assertEqual(forecast.count("Рис. 10"), 1)  # подпись-дубль перед графиком не повторяется
        self.assertIn("table", kinds.values())

    def test_analyst_text_ranks_above_chart_caption(self):
        top = self.index.search("курс рубля", limit=1)[0]
        self.assertEqual(top.metadata["kind"], "text")
        self.assertIn("Мы ожидаем", top.text)
        self.assertEqual({c.kind for c in self.index.chunks}, {"text", "chart", "table"})


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
