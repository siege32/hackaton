from __future__ import annotations

import unittest

from ai_engine.errors import LLMRetriesExhausted, StorageError
from ai_engine.models import JOB_NEWS_PROCESS, NewsJob, ProcessingStatus
from ai_engine.queue.memory import InMemoryQueue
from ai_engine.storage.memory import InMemoryIntelStore
from ai_engine.worker import NewsWorker, run_news_worker
from tests.helpers import FACT_PROMPT, GAS_NEWS, fact_payload, make_service, make_settings


def make_job(news_id: str = GAS_NEWS.news_id, job_id: str = "job-1") -> NewsJob:
    return NewsJob(job_id=job_id, news_id=news_id, job_type=JOB_NEWS_PROCESS)


class FailingSaveStore(InMemoryIntelStore):
    def save_processing_result(self, result):
        raise StorageError("simulated database outage")


class WorkerTests(unittest.TestCase):
    def test_worker_reads_redis_style_queue_and_processes_news(self):
        service, store, *_ = make_service()
        queue = InMemoryQueue()
        queue.enqueue(make_job())
        handled = run_news_worker(service, queue, max_jobs=1)
        self.assertEqual(handled, 1)
        self.assertEqual(store.get_processing_result(GAS_NEWS.news_id).status, ProcessingStatus.PROCESSED)
        self.assertEqual(queue.depth(), 0)
        self.assertEqual(queue.in_flight, {})
        self.assertEqual(queue.dead_letters, [])

    def test_storage_error_does_not_ack_the_job(self):
        store = FailingSaveStore()
        service, *_ = make_service(store=store)
        queue = InMemoryQueue()
        queue.enqueue(make_job())
        reserved = queue.reserve(0)
        worker = NewsWorker(service, queue)
        outcome = worker.handle(reserved)
        self.assertEqual(outcome, "retried")
        self.assertEqual(queue.depth(), 1, "job must be requeued, not acked")
        self.assertEqual(queue.dead_letters, [])
        stored = store.processing.get(GAS_NEWS.news_id)
        self.assertIsNotNone(stored)
        self.assertNotEqual(stored.status, ProcessingStatus.PROCESSED)

    def test_malformed_message_goes_to_dead_letter(self):
        service, *_ = make_service()
        queue = InMemoryQueue()
        queue.enqueue_raw("{not json")
        handled = run_news_worker(service, queue, max_jobs=1)
        self.assertEqual(handled, 1)
        self.assertEqual(len(queue.dead_letters), 1)
        self.assertEqual(queue.dead_letters[0][2], "malformed_job")

    def test_missing_news_retries_are_bounded_and_dead_lettered(self):
        service, *_ = make_service()
        settings = make_settings(worker_max_attempts=2)
        service.settings = settings
        queue = InMemoryQueue()
        queue.enqueue(make_job(news_id="news-does-not-exist", job_id="job-missing"))
        handled = run_news_worker(service, queue, max_jobs=2)
        self.assertEqual(handled, 2)
        self.assertEqual(len(queue.dead_letters), 1)
        self.assertEqual(queue.depth(), 0)
        self.assertIn("news_not_found", queue.dead_letters[0][2] + queue.dead_letters[0][1])

    def test_duplicate_delivery_does_not_duplicate_facts(self):
        service, store, _, _, _, llm = make_service()
        queue = InMemoryQueue()
        queue.enqueue(make_job(job_id="job-dup"))
        queue.enqueue(make_job(job_id="job-dup"))
        handled = run_news_worker(service, queue, max_jobs=2)
        self.assertEqual(handled, 2)
        self.assertEqual(len(store.get_facts(GAS_NEWS.news_id)), 1)
        self.assertEqual(llm.count(FACT_PROMPT), 1)

    def test_stale_reservation_is_recovered_and_processed(self):
        service, store, *_ = make_service()
        queue = InMemoryQueue()
        queue.enqueue(make_job())
        reserved = queue.reserve(0)
        self.assertIsNotNone(reserved)
        queue.expire_reservation(reserved)
        handled = run_news_worker(service, queue, max_jobs=1)
        self.assertEqual(handled, 1)
        self.assertEqual(store.get_processing_result(GAS_NEWS.news_id).status, ProcessingStatus.PROCESSED)
        self.assertEqual(queue.dead_letters, [])

    def test_llm_retry_exhaustion_is_bounded_and_records_failure(self):
        service, store, *_ = make_service(
            responses={FACT_PROMPT: [LLMRetriesExhausted("provider down", attempts=2)]},
            settings=make_settings(worker_max_attempts=2),
        )
        service.settings = service.settings
        queue = InMemoryQueue()
        queue.enqueue(make_job())
        handled = run_news_worker(service, queue, max_jobs=2)
        self.assertEqual(handled, 2)
        self.assertEqual(len(queue.dead_letters), 1)
        result = store.get_processing_result(GAS_NEWS.news_id)
        self.assertEqual(result.status, ProcessingStatus.FAILED)
        self.assertEqual(result.error_code, "llm_retries_exhausted")

    def test_queue_unavailable_does_not_crash_worker(self):
        service, *_ = make_service()
        queue = InMemoryQueue()
        queue.unavailable = True
        handled = run_news_worker(service, queue, max_jobs=1)
        self.assertEqual(handled, 0)

    def test_empty_news_is_acked_after_failure_result_is_saved(self):
        from ai_engine.models import NewsRecord

        empty = NewsRecord(news_id="news-empty", title="", text="", source_id="s")
        service, store, *_ = make_service(news=[empty])
        queue = InMemoryQueue()
        queue.enqueue(make_job(news_id="news-empty"))
        handled = run_news_worker(service, queue, max_jobs=1)
        self.assertEqual(handled, 1)
        self.assertEqual(store.get_processing_result("news-empty").status, ProcessingStatus.FAILED)
        self.assertEqual(queue.in_flight, {})


if __name__ == "__main__":
    unittest.main()
