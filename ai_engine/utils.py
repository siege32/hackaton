"""Small shared helpers (serialization, stable ids, text normalization)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Mapping


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def jsonable(value: Any) -> Any:
    """Recursively convert dataclasses/enums into JSON-serializable values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: jsonable(v) for k, v in value.__dict__.items()}
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return str(value)


def dumps(value: Any) -> str:
    return json.dumps(jsonable(value), ensure_ascii=False, sort_keys=True)


def stable_id(prefix: str, *parts: Any) -> str:
    """Deterministic identifier, used for facts and other deduplicated rows."""
    payload = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def content_version(*parts: Any) -> str:
    payload = "|".join(str(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_WS_RE = re.compile(r"\s+")
_NBSP_RE = re.compile(r"[\u00a0\u2007\u202f]")


def normalize_text(text: str) -> str:
    """Normalize text for quote matching without changing its meaning."""
    if not text:
        return ""
    text = _NBSP_RE.sub(" ", text)
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    return _WS_RE.sub(" ", text).strip().lower()


def contains_quote(source_text: str, quote: str) -> bool:
    """True when ``quote`` occurs in ``source_text`` (whitespace/case tolerant)."""
    if not quote or not source_text:
        return False
    nq = normalize_text(quote).strip(" .;:!?\"'")
    ns = normalize_text(source_text)
    if not nq:
        return False
    return nq in ns


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """Truncate long news text on a word boundary.

    Returns ``(text, was_truncated)``.  The marker is deliberately explicit so
    the model can report missing information instead of inventing the tail.
    """
    if not text or len(text) <= limit:
        return text, False
    head = text[:limit]
    cut = head.rfind(" ")
    if cut > int(limit * 0.8):
        head = head[:cut]
    return head + "\n[...текст новости обрезан из-за ограничения длины...]", True
