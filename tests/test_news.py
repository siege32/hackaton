"""Тесты конвейера новостей — без сети: RSS и страницы подменяются заготовками."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_engine.models import NewsRecord
from ai_engine.queue.memory import InMemoryQueue
from ai_engine.storage.base import NewsRepository
from data_pipeline.news import (SqliteNewsRepository, add_manual_text, cluster_sources, enqueue_pending,
                                fetch_source, list_news, open_news_db, search_news)
from data_pipeline.news.clean import clean_article, normalize_text, normalize_url
from data_pipeline.news.dedup import novelty
from data_pipeline.news.fetch import FetchError, parse_date, parse_rss
from data_pipeline.news.prefilter import DROP, KEEP, NO_TEXT_REASON, REVIEW, prefilter
from data_pipeline.news.sources import NewsSource

NOW = datetime.now(timezone.utc)
PUB = (NOW - timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S +0300")
PUB2 = (NOW - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S +0300")

RATE_TEXT = (
    "Совет директоров Банка России сохранил ключевую ставку на уровне 14% годовых. "
    "Регулятор отметил ускорение устойчивой инфляции до 5–6%. "
    "Стоимость кредитов для бизнеса в ближайшие месяцы не снизится, считают аналитики."
)
RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:yandex="http://news.yandex.ru"><channel>
<item><title>ЦБ сохранил ключевую ставку 14%</title><link>https://news.test/rate?utm_source=rss</link>
  <pubDate>{PUB}</pubDate><category>Экономика</category><description>Коротко о ставке</description>
  <yandex:full-text>{RATE_TEXT}</yandex:full-text></item>
<item><title>Фигуристы взяли золото</title><link>https://news.test/sport</link><pubDate>{PUB}</pubDate>
  <category>Спорт</category><description>Победа на турнире</description>
  <yandex:full-text>Российские фигуристы выиграли турнир, публика аплодировала стоя. Это была яркая победа.</yandex:full-text></item>
</channel></rss>""".encode("utf-8")

PAGE_RSS = f"""<?xml version="1.0"?><rss><channel>
<item><title>Банк России оставил ставку 14%</title><link>https://other.test/a1</link><pubDate>{PUB2}</pubDate>
  <description>ЦБ взял паузу</description></item>
<item><title>Недоступная статья о пошлинах</title><link>https://other.test/broken</link><pubDate>{PUB2}</pubDate>
  <description>Правительство повысит пошлины на импорт</description></item>
</channel></rss>""".encode("utf-8")

PAGE_HTML = f"""<html><head><title>Банк России оставил ставку 14%</title></head><body>
<nav>Главная | Экономика | Подписка</nav>
<article><h1>Банк России оставил ставку 14%</h1>
<p>Банк России на заседании сохранил ключевую ставку на уровне 14% годовых, сообщил регулятор.</p>
<p>Глава ЦБ заявила, что прогноз по ставке на конец 2027 года составляет 12%, а снижение ставки не обсуждалось.</p>
<p>Аналитики ожидают, что кредиты для бизнеса останутся дорогими до конца года.</p>
</article><footer>Все права защищены</footer></body></html>""".encode("utf-8")


def fake_download(pages: dict[str, bytes]):
    def download(url: str) -> bytes:
        if url not in pages:
            raise FetchError(f"HTTP 404 {url}")
        return pages[url]
    return download


class CleanTest(unittest.TestCase):
    def test_url_normalization(self):
        self.assertEqual(normalize_url("https://www.News.test/a/?utm_source=x&id=5#top"), "news.test/a?id=5")
        self.assertEqual(normalize_url("http://m.news.test/a"), normalize_url("https://news.test/a/"))

    def test_boilerplate_removed_numbers_kept(self):
        text = normalize_text("Ставка 14%,  инфляция 5–6%.\n\nНовость дополняется\nПодписывайтесь на наш канал")
        self.assertEqual(text, "Ставка 14%, инфляция 5–6%.")

    def test_title_repeat_and_summary_only(self):
        article = clean_article("Заголовок", "Заголовок\nПервый абзац текста.\nВторой абзац.")
        self.assertEqual((article.lead, article.body), ("Первый абзац текста.", "Второй абзац."))
        self.assertEqual(clean_article("Заголовок", "", "Анонс").quality, "summary_only")

    def test_dates_normalized_to_utc(self):
        self.assertEqual(parse_date("Wed, 22 Oct 2026 10:00:00 +0300"), "2026-10-22T07:00:00+00:00")
        self.assertIsNone(parse_date("вчера"))


class RssTest(unittest.TestCase):
    def test_parse_full_text_namespace(self):
        items = parse_rss(RSS)
        self.assertEqual(len(items), 2)
        self.assertIn("14% годовых", items[0].full_text)
        self.assertEqual(items[0].categories, ["Экономика"])

    def test_broken_xml_is_error(self):
        with self.assertRaises(FetchError):
            parse_rss(b"<rss><channel><item>")


class PrefilterTest(unittest.TestCase):
    def test_economic_news_kept(self):
        self.assertEqual(prefilter("ЦБ сохранил ключевую ставку", RATE_TEXT).decision, KEEP)

    def test_substring_false_topics_ignored(self):
        # «аналогичный» не «налог», «поставки» не «ставки», «ресурс» не «курс»
        result = prefilter("Теннисистка вышла в финал", "Аналогичный результат, поставки мячей, ресурс сил.")
        self.assertNotEqual(result.decision, KEEP)
        self.assertFalse(result.critical)

    def test_infrastructure_goes_to_review(self):
        self.assertEqual(prefilter("Поражены два сухогруза", "Атака по судам.").decision, REVIEW)

    def test_irrelevant_rubric_and_stale(self):
        self.assertEqual(prefilter("Фигуристы взяли золото", "Победа.", categories=["Спорт"]).decision, DROP)
        old = (NOW - timedelta(days=30)).isoformat()
        self.assertEqual(prefilter("ЦБ сохранил ставку", RATE_TEXT, published_at=old).decision, DROP)
        self.assertEqual(prefilter("ЦБ сохранил ставку", RATE_TEXT, published_at=old, manual=True).decision, KEEP)

    def test_summary_only_needs_review(self):
        self.assertEqual(prefilter("Пошлины на импорт", "Анонс", text_quality="summary_only").decision, REVIEW)

    def test_title_only_dropped(self):
        # ТАСС: страница закрыта (403), в RSS нет анонса — остаётся один заголовок.
        result = prefilter("Санкции против банков", "", text_quality="summary_only")
        self.assertEqual(result.decision, DROP)
        self.assertEqual(result.reasons, [NO_TEXT_REASON])


class NoveltyTest(unittest.TestCase):
    def test_new_numbers_and_denial(self):
        known = ["Компания сообщила о сделке. Сумма составила 10 млрд рублей."]
        same = novelty("Компания сообщила о сделке. Сумма составила 10 млрд рублей.", known)
        self.assertFalse(same.adds_facts)
        more = novelty("Компания сообщила о сделке. Сумма составила 12 млрд рублей.", known)
        self.assertIn("12", more.new_numbers)
        denial = novelty("Компания опровергла сообщения о сделке, назвав их не соответствующими действительности.", known)
        self.assertTrue(denial.denial and denial.adds_facts)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_news_db(Path(self.tmp.name) / "news.sqlite")
        self.rss_source = NewsSource("feed", "Лента с полным текстом", "https://news.test/rss", "rss")
        self.page_source = NewsSource("site", "Сайт со страницами", "https://other.test/rss", "page")
        self.download = fake_download({
            "https://news.test/rss": RSS,
            "https://other.test/rss": PAGE_RSS,
            "https://other.test/a1": PAGE_HTML,
        })

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_both(self):
        a = fetch_source(self.conn, self.rss_source, download=self.download)
        b = fetch_source(self.conn, self.page_source, download=self.download)
        return a, b

    def test_textless_record_dropped_on_open(self):
        # Запись без текста, сохранённая старой версией предотбора как REVIEW.
        self.run_both()
        news_id = self.conn.execute("SELECT news_id FROM news LIMIT 1").fetchone()[0]
        with self.conn:
            self.conn.execute("UPDATE news SET text = '', prefilter = 'REVIEW', should_process = 1 "
                              "WHERE news_id = ?", (news_id,))
        self.conn.close()
        self.conn = open_news_db(Path(self.tmp.name) / "news.sqlite")
        row = self.conn.execute("SELECT prefilter, should_process FROM news WHERE news_id = ?",
                                (news_id,)).fetchone()
        self.assertEqual((row[0], row[1]), (DROP, 0))
        others = self.conn.execute("SELECT count(*) FROM news WHERE prefilter != 'DROP'").fetchone()[0]
        self.assertGreater(others, 0)

    def test_fetch_prefilter_cluster_and_novelty(self):
        a, b = self.run_both()
        self.assertEqual((a["status"], a["new"], a["KEEP"], a["DROP"]), ("success", 2, 1, 1))
        self.assertEqual(b["status"], "partial")  # одна страница недоступна
        self.assertEqual(b["page_errors"], 1)
        rows = {r["title"]: r for r in list_news(self.conn, prefilter=(), include_duplicates=True)}
        first, second = rows["ЦБ сохранил ключевую ставку 14%"], rows["Банк России оставил ставку 14%"]
        self.assertEqual(second["cluster_id"], first["news_id"])  # одно событие в двух СМИ
        self.assertIn("2027", second["novelty"]["new_numbers"])  # вторая публикация добавила прогноз
        self.assertTrue(second["novelty"]["adds_facts"])
        broken = rows["Недоступная статья о пошлинах"]
        self.assertEqual((broken["text_quality"], broken["prefilter"]), ("summary_only", REVIEW))
        self.assertEqual(len(cluster_sources(self.conn, first["news_id"])), 2)

    def test_rerun_is_idempotent(self):
        self.run_both()
        a, b = self.run_both()
        self.assertEqual((a["new"], a["updated"], a["unchanged"]), (0, 0, 2))
        self.assertEqual((b["new"], b["updated"], b["unchanged"]), (0, 0, 2))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM news_history").fetchone()[0], 0)

    def test_updated_text_gets_new_version(self):
        fetch_source(self.conn, self.rss_source, download=self.download)
        repo = SqliteNewsRepository(self.conn)
        news_id = list_news(self.conn)[0]["news_id"]
        v1 = repo.get_news(news_id).content_version
        changed = RSS.replace("не снизится".encode("utf-8"), "снизится уже в декабре".encode("utf-8"))
        result = fetch_source(self.conn, self.rss_source, download=fake_download({"https://news.test/rss": changed}))
        self.assertEqual(result["updated"], 1)
        self.assertNotEqual(repo.get_news(news_id).content_version, v1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM news_history").fetchone()[0], 1)

    def test_exact_duplicate_not_processed(self):
        first = add_manual_text(self.conn, "Новость о ставке", RATE_TEXT, url="https://a.test/1")
        copy = add_manual_text(self.conn, "Новость о ставке", RATE_TEXT, url="https://b.test/copy")
        self.assertEqual(copy.status, "duplicate")
        self.assertEqual(copy.details["duplicate_of"], first.news_id)
        self.assertFalse(copy.should_process)

    def test_source_error_differs_from_empty(self):
        error = fetch_source(self.conn, NewsSource("x", "x", "https://down.test/rss", "rss"), download=self.download)
        self.assertEqual(error["status"], "error")
        empty_feed = fake_download({"https://empty.test/rss": b"<rss><channel></channel></rss>"})
        empty = fetch_source(self.conn, NewsSource("y", "y", "https://empty.test/rss", "rss"), download=empty_feed)
        self.assertEqual(empty["status"], "empty")

    def test_repository_matches_ai_engine_contract(self):
        fetch_source(self.conn, self.rss_source, download=self.download)
        repo = SqliteNewsRepository(self.conn)
        self.assertIsInstance(repo, NewsRepository)
        news_id = list_news(self.conn)[0]["news_id"]
        record = repo.get_news(news_id)
        # версия совпадает с той, что NewsRecord посчитал бы сам -> кэш ai_engine согласован
        recomputed = NewsRecord(news_id=record.news_id, title=record.title, text=record.text)
        self.assertEqual(record.content_version, recomputed.content_version)
        self.assertIsNone(repo.get_news("news_missing"))

    def test_manual_news_and_search(self):
        result = add_manual_text(self.conn, "Правительство ввело экспортные пошлины на удобрения",
                                 "С 1 декабря вводятся экспортные пошлины на минеральные удобрения в размере 10%. "
                                 "Мера затронет производителей азотных и калийных удобрений.")
        self.assertEqual(result.prefilter, KEEP)
        found = search_news(self.conn, "пошлины на удобрения")
        self.assertEqual(found["status"], "success")
        self.assertEqual(found["data"][0]["news_id"], result.news_id)
        self.assertEqual(search_news(self.conn, "космический туризм")["status"], "empty")

    def test_enqueue_once_per_version(self):
        self.run_both()
        queue = InMemoryQueue()
        queued = enqueue_pending(self.conn, queue)
        self.assertEqual(queue.depth(), len(queued))
        self.assertGreaterEqual(len(queued), 2)  # первая публикация события + вторая с новыми фактами
        self.assertEqual(enqueue_pending(self.conn, queue), [])  # повторно не ставим
        job = queue.reserve(0.1)
        self.assertEqual(job.payload["job_type"], "news.process")


if __name__ == "__main__":
    unittest.main()
