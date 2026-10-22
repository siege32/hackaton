"""Redis reliable-queue consumer/producer.

Layout (all keys configurable via Settings):

* ``queue_key``      - pending jobs (LIST, producer LPUSH, worker BRPOPLPUSH)
* ``processing_key`` - reserved jobs awaiting ack (LIST)
* ``job_meta_key``   - HASH reservation token -> {started, job_id, raw}
* ``retry_delay_key``- ZSET raw job -> retry timestamp
* ``dead_letter_key``- LIST dead-letter records

Ack semantics: a job remains in the processing list until the caller has
persisted the result.  If the process dies, ``recover_stale`` returns the job
to the queue after the visibility timeout.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from ..config import Settings
from ..errors import QueueUnavailableError
from ..models import NewsJob
from ..utils import jsonable, utcnow_iso
from .base import ReservedJob

logger = logging.getLogger("sxsvn_intel.queue")


class RedisQueue:
    def __init__(self, settings: Settings, *, client=None) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    @property
    def client(self):
        if self._client is None:
            try:
                import redis  # type: ignore
            except ImportError as exc:  # pragma: no cover - depends on env
                raise QueueUnavailableError(
                    "redis package is not installed; install sxsvn-intel[redis]"
                ) from exc
            self._client = redis.Redis.from_url(
                self.settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=10,
            )
        return self._client

    def _guard(self, operation: str, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except QueueUnavailableError:
            raise
        except Exception as exc:  # redis.RedisError and transport errors
            raise QueueUnavailableError(
                f"Redis {operation} failed: {type(exc).__name__}"
            ) from exc

    def ping(self) -> bool:
        try:
            return bool(self._guard("ping", self.client.ping))
        except QueueUnavailableError:
            return False

    def enqueue(self, job: NewsJob) -> str:
        raw = json.dumps(job.to_message(), ensure_ascii=False)
        self._guard("enqueue", self.client.lpush, self.settings.queue_key, raw)
        return job.job_id

    def reserve(self, timeout: float) -> ReservedJob | None:
        client = self.client
        raw = None
        if hasattr(client, "blmove"):
            raw = self._guard(
                "reserve",
                client.blmove,
                self.settings.queue_key,
                self.settings.processing_key,
                timeout,
                "RIGHT",
                "LEFT",
            )
        else:  # pragma: no cover - very old redis-py
            raw = self._guard(
                "reserve",
                client.brpoplpush,
                self.settings.queue_key,
                self.settings.processing_key,
                timeout,
            )
        if raw is None:
            return None
        token = uuid.uuid4().hex
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            payload = None
        meta = {
            "started": time.time(),
            "job_id": payload.get("job_id") if isinstance(payload, dict) else None,
            "raw": raw,
        }
        self._guard("reserve_meta", client.hset, self.settings.job_meta_key, token, json.dumps(meta))
        attempts = int(payload.get("attempts") or 0) if isinstance(payload, dict) else 0
        return ReservedJob(
            raw=raw,
            payload=payload if isinstance(payload, dict) else None,
            job_id=str(meta["job_id"]) if meta["job_id"] else None,
            attempts=attempts,
            token=token,
        )

    def ack(self, job: ReservedJob) -> None:
        pipe = self.client.pipeline()
        pipe.lrem(self.settings.processing_key, 1, job.raw)
        pipe.hdel(self.settings.job_meta_key, job.token)
        self._guard("ack", pipe.execute)

    def retry(self, job: ReservedJob, *, error: str, delay: float = 0.0) -> None:
        client = self.client
        if isinstance(job.payload, dict):
            payload = dict(job.payload)
        else:
            # Malformed jobs should be dead-lettered by the worker; if retry
            # is requested anyway, keep the raw text unchanged.
            payload = None
        attempts = job.attempts + 1
        if payload is not None:
            payload["attempts"] = attempts
            payload["last_error"] = error[:300]
            payload["retried_at"] = utcnow_iso()
            raw = json.dumps(jsonable(payload), ensure_ascii=False)
        else:
            raw = job.raw

        # Remove the reservation and publish its replacement in one Redis
        # transaction. Splitting these operations can permanently lose a job
        # if Redis becomes unavailable after the reservation is deleted.
        pipe = client.pipeline()
        pipe.lrem(self.settings.processing_key, 1, job.raw)
        pipe.hdel(self.settings.job_meta_key, job.token)
        if payload is not None and delay > 0:
            pipe.zadd(self.settings.retry_delay_key, {raw: time.time() + delay})
        else:
            pipe.lpush(self.settings.queue_key, raw)
        self._guard("retry", pipe.execute)

    def dead_letter(self, job: ReservedJob, *, error: str, code: str = "failed") -> None:
        record = json.dumps(
            {
                "raw": job.raw,
                "payload": job.payload,
                "error": error[:1000],
                "code": code,
                "attempts": job.attempts + 1,
                "at": utcnow_iso(),
            },
            ensure_ascii=False,
        )
        pipe = self.client.pipeline()
        pipe.lrem(self.settings.processing_key, 1, job.raw)
        pipe.hdel(self.settings.job_meta_key, job.token)
        pipe.lpush(self.settings.dead_letter_key, record)
        self._guard("dead_letter", pipe.execute)

    def recover_stale(self, visibility_timeout: float, max_attempts: int) -> int:
        now = time.time()
        meta = self._guard("recover_scan", self.client.hgetall, self.settings.job_meta_key) or {}
        recovered = 0
        for token, value in meta.items():
            try:
                info = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                info = None
            if not isinstance(info, dict):
                continue
            try:
                started = float(info.get("started") or now)
            except (TypeError, ValueError):
                # Ignore corrupt reservation metadata; a later cleanup can
                # inspect it without blocking recovery of all other jobs.
                continue
            if now - started <= visibility_timeout:
                continue
            raw = info.get("raw")
            payload = info.get("payload")
            if payload is None:
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    payload = None
            if not isinstance(payload, dict):
                payload = None
            try:
                attempts = int((payload or {}).get("attempts") or 0) + 1
            except (TypeError, ValueError):
                attempts = 1
            pipe = self.client.pipeline()
            pipe.lrem(self.settings.processing_key, 1, raw)
            pipe.hdel(self.settings.job_meta_key, token)
            if payload is not None and attempts < max_attempts:
                payload["attempts"] = attempts
                payload["last_error"] = "recovered stale reservation"
                payload["retried_at"] = utcnow_iso()
                pipe.lpush(self.settings.queue_key, json.dumps(jsonable(payload), ensure_ascii=False))
            else:
                pipe.lpush(
                    self.settings.dead_letter_key,
                    json.dumps(
                        {
                            "raw": raw,
                            "payload": payload,
                            "error": "stale reservation exceeded retry limit",
                            "code": "stale",
                            "attempts": attempts,
                            "at": utcnow_iso(),
                        },
                        ensure_ascii=False,
                    ),
                )
            self._guard("recover_stale", pipe.execute)
            recovered += 1
        return recovered

    def promote_delayed(self) -> int:
        now = time.time()
        items = self._guard(
            "promote_scan",
            self.client.zrangebyscore,
            self.settings.retry_delay_key,
            "-inf",
            now,
            start=0,
            num=100,
        ) or []
        for raw in items:
            pipe = self.client.pipeline()
            pipe.zrem(self.settings.retry_delay_key, raw)
            pipe.lpush(self.settings.queue_key, raw)
            self._guard("promote_delayed", pipe.execute)
        return len(items)

    def depth(self) -> int:
        return int(self._guard("depth", self.client.llen, self.settings.queue_key) or 0)
