"""Reliable job queue abstraction.

The module consumes the existing Redis queue.  Jobs are reserved into a
processing list, acknowledged only after the result is persisted, and moved to
a dead-letter list after bounded retries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..models import NewsJob


@dataclass
class ReservedJob:
    raw: str
    payload: dict[str, Any] | None
    job_id: str | None
    attempts: int
    token: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def malformed(self) -> bool:
        return self.payload is None


@runtime_checkable
class JobQueue(Protocol):
    def enqueue(self, job: NewsJob) -> str: ...

    def reserve(self, timeout: float) -> ReservedJob | None: ...

    def ack(self, job: ReservedJob) -> None: ...

    def retry(self, job: ReservedJob, *, error: str, delay: float = 0.0) -> None: ...

    def dead_letter(self, job: ReservedJob, *, error: str, code: str = "failed") -> None: ...

    def recover_stale(self, visibility_timeout: float, max_attempts: int) -> int: ...

    def promote_delayed(self) -> int: ...

    def depth(self) -> int: ...

    def ping(self) -> bool: ...
