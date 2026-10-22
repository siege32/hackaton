from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JobStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS api_jobs (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def create(self, job_id: str, job_type: str, payload: dict[str, Any]) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO api_jobs VALUES (?, ?, 'queued', 'В очереди на выполнение', 0, ?, NULL, NULL, ?, ?)",
                (job_id, job_type, json.dumps(payload, ensure_ascii=False), now, now),
            )

    def update(
        self,
        job_id: str,
        *,
        status: str,
        stage: str,
        progress: float,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE api_jobs
                   SET status = ?, stage = ?, progress = ?, result_json = ?, error = ?, updated_at = ?
                   WHERE job_id = ?""",
                (
                    status,
                    stage,
                    progress,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    self._now(),
                    job_id,
                ),
            )

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM api_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        serialized_result = result.pop("result_json")
        result["result"] = json.loads(serialized_result) if serialized_result else None
        return result

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")