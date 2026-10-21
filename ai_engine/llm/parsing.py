"""Robust extraction of a single JSON object from an LLM response."""

from __future__ import annotations

import json
import re
from typing import Any

from ..errors import LLMInvalidResponseError

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip())


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first complete JSON object found in ``text``.

    LLMs occasionally wrap JSON in Markdown fences or add a sentence before or
    after it.  We do not attempt to repair malformed JSON: only clean
    extraction is allowed.
    """
    if not isinstance(text, str) or not text.strip():
        raise LLMInvalidResponseError("LLM returned an empty response")
    cleaned = _strip_fences(text)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        raise LLMInvalidResponseError("LLM response is valid JSON but not an object")
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = cleaned.find("{", start + 1)

    raise LLMInvalidResponseError(
        "LLM response does not contain a parseable JSON object",
        details={"preview": text[:300]},
    )
