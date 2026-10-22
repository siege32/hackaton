"""Redis worker: reserve -> process -> persist -> ack.

Critical ordering: ``ack`` happens only after ``process_news`` /
``classify_news_for_company`` returned successfully and the result was saved.
Storage failures therefore leave the job in the processing list for recovery.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .config import Settings
from .errors import (
    CompanyNotFoundError,
    ConfigError,
    IntelError,
    LLMError,
    NewsNotFoundError,
    ProcessingNotReadyError,
    QueueUnavailableError,
    SchemaValidationError,
    StorageError,
)
from .models import (
    JOB_COMPANY_ANALYZE,
    JOB_COMPANY_ASSESS,
    JOB_NEWS_PROCESS,
    NewsJob,
)
from .queue.base import JobQueue, ReservedJob
from .queue.redis_queue import RedisQueue
from .service import NewsIntelligenceService

logger = logging.getLogger("sxsvn_intel.worker")


class NewsWorker:
    def __init__(
        self,
        service: NewsIntelligenceService,
        queue: JobQueue,
        *,
        settings: Settings | None = None,
        sleep: Callable[[float], None] = time.sleep,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.service = service
        self.queue = queue
        self.settings = settings or service.settings
        self._sleep = sleep
        self._logger = logger_ or logger

    # ------------------------------------------------------------------
    def run(
        self,
        *,
        max_jobs: int | None = None,
        stop_event: Any | None = None,
        block_timeout: float | None = None,
    ) -> int:
        timeout = (
            self.settings.worker_block_timeout if block_timeout is None else block_timeout
        )
        handled = 0
        while not self._stop_requested(stop_event):
            try:
                self.queue.recover_stale(
                    self.settings.worker_visibility_timeout, self.settings.worker_max_attempts
                )
                self.queue.promote_delayed()
            except QueueUnavailableError:
                self._logger.warning("queue recovery unavailable; will retry")
                if max_jobs is not None:
                    break
                self._sleep(self.settings.worker_retry_delay)
                continue

            try:
                reserved = self.queue.reserve(timeout)
            except QueueUnavailableError:
                self._logger.warning("queue reserve unavailable; will retry")
                if max_jobs is not None:
                    break
                self._sleep(self.settings.worker_retry_delay)
                continue

            if reserved is None:
                if max_jobs is not None:
                    break
                continue

            self.handle(reserved)
            handled += 1
            if max_jobs is not None and handled >= max_jobs:
                break
        return handled

    # ------------------------------------------------------------------
    def handle(self, reserved: ReservedJob) -> str:
        started = time.monotonic()
        if reserved.malformed:
            self._dead_letter(reserved, "malformed queue message", "malformed_job", job=None)
            return "dead_letter"

        try:
            job = NewsJob.from_message(reserved.payload or {})
        except Exception as exc:
            self._dead_letter(
                reserved,
                f"invalid queue message: {exc}",
                "malformed_job",
                job=None,
            )
            return "dead_letter"

        self._logger.info(
            "job reserved job_id=%s type=%s news_id=%s company_id=%s attempt=%s",
            job.job_id,
            job.job_type,
            job.news_id,
            job.company_id,
            reserved.attempts + 1,
        )
        try:
            self._dispatch(job)
        except Exception as exc:  # noqa: BLE001 - explicit mapping below
            return self._handle_failure(reserved, job, exc, started)
        else:
            try:
                self.queue.ack(reserved)
            except Exception as exc:  # queue unavailable / network
                self._logger.error(
                    "ack failed job_id=%s news_id=%s error=%s; job will be recovered",
                    job.job_id,
                    job.news_id,
                    type(exc).__name__,
                )
                return "ack_failed"
            self._logger.info(
                "job acked job_id=%s type=%s news_id=%s elapsed_ms=%s",
                job.job_id,
                job.job_type,
                job.news_id,
                int((time.monotonic() - started) * 1000),
            )
            return "acked"

    def _dispatch(self, job: NewsJob) -> None:
        if job.job_type == JOB_NEWS_PROCESS:
            self.service.process_news(
                job.news_id,
                content_version=job.content_version,
                job_id=job.job_id,
            )
            return
        if job.job_type == JOB_COMPANY_ASSESS:
            assert job.company_id is not None
            self.service.classify_news_for_company(
                job.news_id,
                job.company_id,
                job_id=job.job_id,
            )
            return
        if job.job_type == JOB_COMPANY_ANALYZE:
            assert job.company_id is not None
            event_ids = job.payload.get("event_ids") or [job.news_id]
            if isinstance(event_ids, str):
                event_ids = [event_ids]
            self.service.analyze_company_impact(
                job.company_id,
                event_ids,
                include_review=bool(job.payload.get("include_review", True)),
                include_drop=bool(job.payload.get("include_drop", False)),
                auto_classify=True,
            )
            return
        raise ConfigError(f"unsupported job type {job.job_type!r}")

    # ------------------------------------------------------------------
    def _handle_failure(
        self,
        reserved: ReservedJob,
        job: NewsJob,
        exc: Exception,
        started: float,
    ) -> str:
        code = getattr(exc, "code", type(exc).__name__)
        message = str(exc)[:800]
        attempts = reserved.attempts + 1
        retryable = self._is_retryable(exc)
        permanent = not retryable
        self._logger.warning(
            "job failed job_id=%s type=%s news_id=%s attempt=%s/%s code=%s retryable=%s error=%s",
            job.job_id,
            job.job_type,
            job.news_id,
            attempts,
            self.settings.worker_max_attempts,
            code,
            retryable,
            message,
        )

        if permanent or attempts >= self.settings.worker_max_attempts:
            if job.job_type == JOB_NEWS_PROCESS:
                try:
                    self.service.record_failed_processing(
                        job.news_id,
                        content_version=job.content_version or "",
                        error_code=str(code),
                        error_message=message,
                        job_id=job.job_id,
                    )
                except Exception as record_exc:  # storage is down
                    self._logger.error(
                        "failed to persist terminal failure news_id=%s error=%s",
                        job.news_id,
                        type(record_exc).__name__,
                    )
            self._dead_letter(reserved, message, str(code), job=job)
            return "dead_letter"

        try:
            self.queue.retry(
                reserved, error=message, delay=self.settings.worker_retry_delay
            )
        except QueueUnavailableError:
            self._logger.error(
                "retry publish failed job_id=%s; job remains in processing for recovery",
                job.job_id,
            )
            return "retry_deferred"
        return "retried"

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(
            exc,
            (
                CompanyNotFoundError,
                ConfigError,
                ProcessingNotReadyError,
            ),
        ):
            return False
        if isinstance(exc, (NewsNotFoundError,)):
            # News may appear slightly later if the producer and worker race.
            return True
        if isinstance(
            exc,
            (
                LLMError,
                SchemaValidationError,
                StorageError,
                QueueUnavailableError,
            ),
        ):
            return True
        if isinstance(exc, IntelError):
            return True
        return True

    def _dead_letter(
        self,
        reserved: ReservedJob,
        error: str,
        code: str,
        *,
        job: NewsJob | None,
    ) -> None:
        try:
            self.queue.dead_letter(reserved, error=error, code=code)
        except Exception as exc:  # pragma: no cover - Redis outage during DLQ
            self._logger.error(
                "dead-letter publish failed job_id=%s error=%s",
                job.job_id if job else None,
                type(exc).__name__,
            )
        else:
            self._logger.error(
                "job dead-lettered job_id=%s news_id=%s code=%s error=%s",
                job.job_id if job else None,
                job.news_id if job else None,
                code,
                error,
            )

    @staticmethod
    def _stop_requested(stop_event: Any | None) -> bool:
        if stop_event is None:
            return False
        if hasattr(stop_event, "is_set"):
            return bool(stop_event.is_set())
        if callable(stop_event):
            return bool(stop_event())
        return bool(stop_event)


def run_news_worker(
    service: NewsIntelligenceService,
    queue: JobQueue | None = None,
    *,
    max_jobs: int | None = None,
    stop_event: Any | None = None,
    block_timeout: float | None = None,
) -> int:
    """Run the consumer of the existing Redis queue.

    Blocks until ``stop_event`` is set, ``max_jobs`` jobs are handled, or (when
    ``max_jobs`` is set for tests) the queue becomes empty.
    """
    queue = queue or RedisQueue(service.settings)
    worker = NewsWorker(service, queue)
    return worker.run(
        max_jobs=max_jobs, stop_event=stop_event, block_timeout=block_timeout
    )
