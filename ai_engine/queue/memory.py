"""In-memory queue with the same semantics as the Redis implementation.

It simulates reservation, ack, bounded retry, delayed retry, stale recovery,
duplicate delivery and dead-lettering so the worker can be tested without
Redis.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from ..models import NewsJob
from ..utils import jsonable, utcnow_iso
from .base import ReservedJob


@dataclass
class _Item:
    raw: str
    payload: dict | None
    attempts: int = 0


@dataclass
class _InFlight:
    item: _Item
    started: float
    token: str


class InMemoryQueue:
    def __init__(self, *, clock=time.monotonic) -> None:
        self.ready: list[_Item] = []
        self.in_flight: dict[str, _InFlight] = {}
        self.delayed: list[tuple[float, _Item]] = []
        self.dead_letters: list[tuple[dict | None, str, str]] = []
        self._clock = clock
        self.unavailable = False

    # -- producer --------------------------------------------------------
    def enqueue(self, job: NewsJob) -> str:
        self.ready.append(_Item(raw=json.dumps(job.to_message(), ensure_ascii=False), payload=job.to_message(), attempts=job.attempts))
        return job.job_id

    def enqueue_raw(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            payload = None
        self.ready.append(_Item(raw=raw, payload=payload))

    # -- consumer --------------------------------------------------------
    def reserve(self, timeout: float) -> ReservedJob | None:
        if self.unavailable:
            from ..errors import QueueUnavailableError

            raise QueueUnavailableError("simulated Redis outage")
        if not self.ready:
            return None
        item = self.ready.pop(0)
        token = uuid.uuid4().hex
        self.in_flight[token] = _InFlight(item=item, started=self._clock(), token=token)
        job_id = item.payload.get("job_id") if isinstance(item.payload, dict) else None
        return ReservedJob(
            raw=item.raw,
            payload=item.payload,
            job_id=str(job_id) if job_id else None,
            attempts=item.attempts,
            token=token,
        )

    def ack(self, job: ReservedJob) -> None:
        self.in_flight.pop(job.token, None)

    def retry(self, job: ReservedJob, *, error: str, delay: float = 0.0) -> None:
        flight = self.in_flight.pop(job.token, None)
        if flight is None:
            return
        item = flight.item
        item.attempts += 1
        raw, payload = self._with_attempts(item, error)
        item.raw, item.payload = raw, payload
        if delay > 0:
            self.delayed.append((self._clock() + delay, item))
        else:
            self.ready.append(item)

    def dead_letter(self, job: ReservedJob, *, error: str, code: str = "failed") -> None:
        flight = self.in_flight.pop(job.token, None)
        payload = flight.item.payload if flight else job.payload
        self.dead_letters.append((payload, error, code))

    def recover_stale(self, visibility_timeout: float, max_attempts: int) -> int:
        now = self._clock()
        recovered = 0
        for token, flight in list(self.in_flight.items()):
            if now - flight.started <= visibility_timeout:
                continue
            self.in_flight.pop(token, None)
            item = flight.item
            item.attempts += 1
            if item.attempts >= max_attempts:
                self.dead_letters.append((item.payload, "stale reservation exceeded retry limit", "stale"))
            else:
                raw, payload = self._with_attempts(item, "recovered stale reservation")
                item.raw, item.payload = raw, payload
                self.ready.append(item)
            recovered += 1
        return recovered

    def promote_delayed(self) -> int:
        now = self._clock()
        due = [(when, item) for when, item in self.delayed if when <= now]
        self.delayed = [(when, item) for when, item in self.delayed if when > now]
        for _, item in due:
            self.ready.append(item)
        return len(due)

    def depth(self) -> int:
        return len(self.ready)

    def ping(self) -> bool:
        return not self.unavailable

    # -- test helpers ----------------------------------------------------
    def redeliver(self, job: ReservedJob) -> None:
        """Simulate broker redelivery of an already reserved (unacked) job."""
        flight = self.in_flight.pop(job.token, None)
        if flight is not None:
            self.ready.append(flight.item)

    def expire_reservation(self, job: ReservedJob, *, seconds_ago: float = 10_000) -> None:
        flight = self.in_flight.get(job.token)
        if flight is not None:
            flight.started = self._clock() - seconds_ago

    @staticmethod
    def _with_attempts(item: _Item, error: str) -> tuple[str, dict | None]:
        if not isinstance(item.payload, dict):
            return item.raw, None
        payload = dict(item.payload)
        payload["attempts"] = item.attempts
        payload["last_error"] = error[:300]
        payload["retried_at"] = utcnow_iso()
        return json.dumps(jsonable(payload), ensure_ascii=False), payload
