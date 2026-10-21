from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

from openai import APIConnectionError, APIStatusError, OpenAI

from .config import Settings, get_settings
from .llm_constants import ALLOWED_MODELS, CHANGELLENGE_BASE_URL, DEFAULT_MODEL

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """A safe, application-level error from the model adapter."""


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


# Backward-compatible name for callers that used the previous adapter.
LLMConfig = Settings


class OpenAICompatibleClient:
    """Transport adapter for the approved OpenAI-compatible endpoint."""

    def __init__(self, config: Settings | None = None, client: OpenAI | None = None):
        self.config = config or get_settings()
        try:
            self.config.validate_model()
        except ValueError as exc:
            raise LLMError(str(exc)) from exc
        self._client = client

    def _client_or_create(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(
                api_key=self.config.llm_api_key,
                base_url=self.config.llm_base_url.rstrip("/"),
                timeout=self.config.llm_timeout,
                max_retries=self.config.llm_max_retries,
            )
        return self._client

    def complete(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        if not self.config.llm_api_key:
            raise LLMError("SXSVN_LLM_API_KEY is required")
        input_chars = len(json.dumps(messages, ensure_ascii=False))
        if input_chars > self.config.llm_max_input_chars:
            raise LLMError(f"Input exceeds {self.config.llm_max_input_chars} characters")
        started = time.monotonic()
        try:
            response = self._client_or_create().chat.completions.create(
                model=self.config.llm_model,
                messages=messages,
                max_tokens=self.config.llm_max_output_tokens,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if not content:
                raise LLMError("Model returned an empty response")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise LLMError("Model response must be a JSON object")
        except (APIConnectionError, APIStatusError) as exc:
            status = getattr(exc, "status_code", "unknown")
            self._log_call("error", input_chars, started, error_type=type(exc).__name__)
            raise LLMError(f"LLM API request failed (status={status})") from exc
        except json.JSONDecodeError as exc:
            self._log_call("error", input_chars, started, error_type=type(exc).__name__)
            raise LLMError("Model returned invalid JSON") from exc
        except (IndexError, KeyError, TypeError, AttributeError) as exc:
            self._log_call("error", input_chars, started, error_type=type(exc).__name__)
            raise LLMError("Model returned an unexpected response shape") from exc
        except LLMError:
            self._log_call("error", input_chars, started, error_type="LLMError")
            raise
        self._log_call("ok", input_chars, started, output_chars=len(content))
        return result

    def _log_call(
        self,
        status: str,
        input_chars: int,
        started: float,
        **extra: Any,
    ) -> None:
        logger.info(
            "llm_call",
            extra={
                "model": self.config.llm_model,
                "input_chars": input_chars,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "status": status,
                **extra,
            },
        )
