"""Тесты модуля данных на маленьких синтетических CSV (без данных кейса)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_engine.models import RelationType
from ai_engine.storage.base import CalculationRepository, CompanyRepository, RelationshipRepository
from data_pipeline import okved
from data_pipeline.exposure import (
    PaymentCalculationRepository,
    calculate_sector_exposure,
    get_counterparty_exposure,
)
from data_pipeline.profiles import SqliteCompanyRepository, get_company_profile
from data_pipeline.relationships import PaymentRelationshipRepository
from data_pipeline.store import build_database, connect

_COMPANY_HEADER = (
    "SYNTHETIC_U;ORG_TYPE;OKFS_NAME;COMPANY_AGE_YEARS_GRADATION;ECO_MARK_LABEL;MAINOKVED_CODE;"
    "MAINOKVED_NAME;NEGATIVEMARK;AMOUNT_WORKERS_FNS_GRADATION;AMOUNT_WORKERS_SPARK_GRADATION;CL_TYPE;"
    "CL_PRODUCT_RKO_MARK;CL_PRODUCT_GAR_MARK;CL_PRODUCT_LOA_MARK;CL_PRODUCT_LEA_MARK;CL_PRODUCT_DEP_MARK;"
    "CL_PRODUCT_ZPP_MARK;FTS_FLAG;FTS_COUNTRY_NAME;FACTORING_MARK;REVENUE_COMPANY;GROUP_ID;CITY;REGION"
)


def _company(cid, code, name, revenue, *, loa="0.0", fts="0.0", countries="", group="g1", city="г. Москва"):
    return (
        f"{cid};КОММ_ОРГ;ЧАСТНАЯ СОБСТВЕННОСТЬ;8-15;Оптимизация;{code};{name};0.0;16-100;;CLIENT;"
        f"1.0;0.0;{loa};0.0;0.0;0.0;{fts};{countries};0.0;{revenue};{group};{city};Москва"
    )


COMPANIES = "\n".join([
    _COMPANY_HEADER,
    _company("U_1", "46.71", "Торговля оптовая топливом", "20000000000.0",
             loa="1.0", fts="1.0", countries="КИТАЙ, ТУРЦИЯ", group="g1"),
    _company("U_2", "6.10", "Добыча сырой нефти", "5000000.0", group="g1"),
    _company("U_3", "41.2", "Строительство зданий", "1000000.0", group="g2"),
    _company("U_4", "19.20", "Производство нефтепродуктов", "3000000.0", group="g3"),
    _company("U_DUP", "46.90", "Торговля", "1.0", group="g4"),
    _company("U_DUP", "52.29", "Перевозки", "2.0", group="g5"),
]) + "\n"

# U_1 получает от U_2 (нефть) 600, от U_3 (стройка) 300, от X (нет в справочнике) 100.
# U_1 платит U_4 (нефтепродукты) 50 двумя строками и U_DUP 50; самоплатёж исключается.
PAYMENTS = "\n".join([
    "BENEFICIARY;PAYER;PAY_AMT;PAY_CNT",
    "U_1;U_2;600000.0;6",
    "U_1;U_3;300000.0;3",
    "U_1;X_UNKNOWN;100000.0;1",
    "U_4;U_1;20000.0;1",
    "U_4;U_1;30000.0;2",
    "U_DUP;U_1;50000.0;1",
    "U_1;U_1;999999000.0;5",
]) + "\n"

PERSONS = "\n".join([
    "SYNTHETIC_F;CITY_NAME;RC_NAME;SYNTHETIC_U",
    "F_1;Москва;МОСКОВСКИЙ;U_1",
    "F_1;Москва;МОСКОВСКИЙ;U_3",
    "F_2;Омск;СИБИРСКИЙ;U_2",
]) + "\n"


class OkvedTest(unittest.TestCase):
    def test_section_and_normalization(self):
        self.assertEqual(okved.normalize_code(" 6.10 "), "06.10")
        self.assertEqual(okved.section("06.10"), "B")
        self.assertEqual(okved.section("46.71.2"), "G")
        self.assertIsNone(okved.section("abc"))

    def test_prefix_matching_is_hierarchical(self):
        self.assertTrue(okved.matches("46.71.2", ["46.7"]))
        self.assertTrue(okved.matches("46.71", ["G"]))
        self.assertFalse(okved.matches("46.90", ["46.7"]))
        self.assertTrue(okved.matches("19.20", ["19"]))
        self.assertFalse(okved.matches("19.20", ["1"]))  # '1' -> '01', не префикс '19'


class DataPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        raw = Path(cls.tmp.name) / "raw"
        raw.mkdir()
        (raw / "df_Uid_data_hack.csv").write_text(COMPANIES, encoding="utf-8")
        (raw / "df_Uid_payment_hack.csv").write_text(PAYMENTS, encoding="utf-8")
        (raw / "df_Fid_Uid_link_hack.csv").write_text(PERSONS, encoding="utf-8")
        cls.db = Path(cls.tmp.name) / "processed" / "companies.sqlite"
        cls.report = build_database(raw, cls.db)
        cls.conn = connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def test_quality_report(self):
        companies, payments = self.report["companies"], self.report["payments"]
        self.assertEqual(companies["companies_loaded"], 4)
        self.assertEqual(companies["large_business_companies"], 1)
        self.assertEqual(companies["ambiguous_ids"], [{"company_id": "U_DUP", "rows": 2}])
        self.assertEqual(payments["self_payment_rows_excluded"], 1)
        self.assertEqual(payments["duplicate_pairs_merged"], 1)
        self.assertEqual(payments["pairs_loaded"], 5)

    def test_rebuild_is_reproducible(self):
        raw = Path(self.tmp.name) / "raw"
        other = Path(self.tmp.name) / "again.sqlite"
        report = build_database(raw, other)
        for key in ("companies", "payments", "person_links"):
            self.assertEqual(report[key], self.report[key])

    def test_adapters_match_ai_engine_protocols(self):
        self.assertIsInstance(SqliteCompanyRepository(self.conn), CompanyRepository)
        self.assertIsInstance(PaymentRelationshipRepository(self.conn), RelationshipRepository)
        self.assertIsInstance(PaymentCalculationRepository(self.conn), CalculationRepository)

    def test_profile_contents_and_provenance(self):
        profile = SqliteCompanyRepository(self.conn).get_company("U_1")
        self.assertEqual(profile.industry, okved.SECTION_NAMES["G"])
        self.assertIn("46.71", profile.activities[0])
        self.assertTrue(any("кредит" in r for r in profile.key_resources))
        self.assertTrue(any("ставк" in d for d in profile.cost_drivers))
        self.assertIn("ВЭД: Китай", profile.geography)
        self.assertEqual(profile.financials["revenue"]["value"], 20_000_000_000.0)
        self.assertEqual(profile.products_services, [])  # нет в данных — не выдумываем
        self.assertTrue(profile.data_sources)
        self.assertTrue(all("field" in s for s in profile.data_sources))
        self.assertTrue(profile.raw["missing_data"])
        # профиль воспроизводим: одинаковые данные -> одинаковая версия
        again = SqliteCompanyRepository(self.conn).get_company("U_1")
        self.assertEqual(profile.profile_version, again.profile_version)

    def test_unknown_and_ambiguous_company(self):
        repo = SqliteCompanyRepository(self.conn)
        self.assertIsNone(repo.get_company("U_404"))
        self.assertIsNone(repo.get_company("U_DUP"))
        result = get_company_profile(self.conn, "U_DUP")
        self.assertEqual(result["status"], "empty")
        self.assertIn("неоднозначен", result["message"])

    def test_payment_links_are_not_confirmed_dependencies(self):
        rels = PaymentRelationshipRepository(self.conn).list_relationships("U_1")
        payments = [r for r in rels if r.relation_type == RelationType.PAYMENT_LINK]
        self.assertTrue(payments)
        self.assertTrue(all(not r.confirmed for r in rels))
        self.assertTrue(all(r.dependency_evidence() == "none" for r in payments))
        top = payments[0]
        self.assertEqual((top.counterparty_id, top.direction), ("U_2", "incoming"))
        self.assertAlmostEqual(top.share, 0.6)
        kinds = {r.metadata["kind"] for r in rels}
        self.assertIn("shared_person", kinds)  # U_1 и U_3 связаны через F_1
        self.assertIn("same_group", kinds)  # U_1 и U_2 в группе g1
        labels = {r.metadata["kind"]: r.metadata["label"] for r in rels if r.metadata["kind"] != "payment"}
        self.assertEqual(labels, {"shared_person": "общие связанные физлица", "same_group": "одна группа компаний"})
        self.assertEqual(top.metadata["label"], "входящие платежи")

    def test_exposure_with_unidentified_counterparties(self):
        result = calculate_sector_exposure(self.conn, "U_1", ["B"], "incoming")
        self.assertEqual(result["status"], "partial")
        self.assertAlmostEqual(result["value"], 0.6)
        self.assertAlmostEqual(result["value_upper_bound"], 0.7)
        self.assertAlmostEqual(result["coverage"], 0.9)
        self.assertEqual(result["affected_counterparties"][0]["counterparty_id"], "U_2")
        self.assertIn("formula", result)

    def test_exposure_outgoing_merges_duplicates(self):
        result = calculate_sector_exposure(self.conn, "U_1", ["19"], "outgoing")
        self.assertAlmostEqual(result["value"], 0.5)  # 50 из 100; U_DUP неидентифицирован
        self.assertEqual(result["affected_counterparties"][0]["amount"], 50000.0)

    def test_exposure_empty_and_error_statuses(self):
        self.assertEqual(calculate_sector_exposure(self.conn, "U_3", ["B"], "incoming")["status"], "empty")
        self.assertIsNone(calculate_sector_exposure(self.conn, "U_3", ["B"], "incoming")["value"])
        self.assertEqual(calculate_sector_exposure(self.conn, "U_404", ["B"], "incoming")["status"], "error")
        self.assertEqual(calculate_sector_exposure(self.conn, "U_1", [], "incoming")["status"], "error")

    def test_counterparty_exposure_tool_and_calculations(self):
        tool = get_counterparty_exposure(self.conn, "U_1")
        self.assertEqual(tool["status"], "partial")
        sectors = {s["okved_section"]: s["share"] for s in tool["data"]["incoming"]["sectors"]}
        self.assertAlmostEqual(sectors["B"], 0.6)
        self.assertAlmostEqual(sectors[None], 0.1)
        calcs = PaymentCalculationRepository(self.conn).list_calculations("U_1")
        self.assertEqual({c["calculation_id"] for c in calcs},
                         {"breakdown:U_1:incoming", "breakdown:U_1:outgoing"})

    def test_list_companies_defaults_to_large_business(self):
        repo = SqliteCompanyRepository(self.conn)
        self.assertEqual([c["company_id"] for c in repo.list_companies()], ["U_1"])
        self.assertEqual(len(repo.list_companies(min_revenue=None)), 4)
        name = repo.list_companies()[0]["display_name"]
        self.assertIn(" · ", name)
        self.assertTrue(name.endswith(" млрд"), name)
        self.assertNotIn("U_1", name)


class DemoDataTest(unittest.TestCase):
    """Демо-набор для жюри: собирается, детерминирован, совпадает с data/demo/."""

    def test_demo_builds_and_matches_committed_files(self):
        from data_pipeline.demo import DEMO_DIR, generate_demo

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "demo"
            counts = generate_demo(out)
            self.assertEqual(counts["large_companies"], 12)
            committed = Path(__file__).resolve().parents[1] / DEMO_DIR
            for csv_file in sorted(out.glob("*.csv")):
                self.assertEqual(csv_file.read_text(encoding="utf-8").splitlines(),
                                 (committed / csv_file.name).read_text(encoding="utf-8").splitlines(),
                                 f"{csv_file.name} устарел: python -m data_pipeline demo-data")

            report = build_database(out, Path(tmp) / "demo.sqlite")
            self.assertEqual(report["companies"]["large_business_companies"], 12)
            self.assertEqual(report["companies"]["ambiguous_ids"], [])
            self.assertEqual(report["payments"]["self_payment_rows_excluded"], 1)
            self.assertGreater(report["person_links"]["persons_linked_to_several_companies"], 0)

            conn = connect(Path(tmp) / "demo.sqlite")
            try:
                fuel = get_counterparty_exposure(conn, "U_DEMO_001", ["B", "19"])
                self.assertGreater(fuel["data"]["outgoing"]["value"], 0.2)
                rels = PaymentRelationshipRepository(conn).list_relationships("U_DEMO_002", limit=50)
                kinds = {r.metadata.get("kind") for r in rels}
                self.assertTrue({"payment", "shared_person", "same_group"} <= kinds)
                # При коротком лимите (интерфейс берёт 12) сначала платёжные контрагенты.
                short = PaymentRelationshipRepository(conn).list_relationships("U_DEMO_002", limit=6)
                self.assertEqual([r.metadata["kind"] for r in short[:4]], ["payment"] * 4)
                self.assertNotEqual(short[-1].metadata["kind"], "payment")

                profile = SqliteCompanyRepository(conn).get_company("U_DEMO_001")
                self.assertEqual(profile.geography[0], "г. Москва")
                self.assertNotIn("Москва", profile.geography)  # «г. Москва · Москва» без повтора
                self.assertNotIn("csv_row", profile.raw)  # служебные коды CSV не уходят в LLM
                self.assertRegex(profile.updated_at, r"^\d{4}-\d{2}-\d{2}$")  # дата сборки базы
                self.assertNotIn("prod_lea", str(profile.to_dict()))
            finally:
                conn.close()


class SetupErrorsTest(unittest.TestCase):
    """Ошибки первого запуска: нет CSV, PDF отчётов лежат не в той папке."""

    def test_build_without_csv_gives_hint_and_exit_code(self):
        import contextlib
        import io

        from data_pipeline.__main__ import main

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError) as ctx:
                build_database(tmp, Path(tmp) / "db.sqlite")
            self.assertIn("data/demo", str(ctx.exception))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = main(["--raw-dir", tmp, "--db", str(Path(tmp) / "db.sqlite"), "build"])
            self.assertEqual(code, 2)
            self.assertIn("df_Uid_data_hack.csv", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_find_pdf_in_reports_dir_or_raw_dir(self):
        from data_pipeline.reports.ocr import _find_pdf

        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            reports = raw / "bank_reports"
            (raw / "a.pdf").write_bytes(b"%PDF")
            self.assertEqual(_find_pdf(reports, "a.pdf"), raw / "a.pdf")  # папки bank_reports нет
            reports.mkdir()
            (reports / "a.pdf").write_bytes(b"%PDF")
            self.assertEqual(_find_pdf(reports, "a.pdf"), reports / "a.pdf")
            self.assertIsNone(_find_pdf(reports, "b.pdf"))


if __name__ == "__main__":
    unittest.main()
