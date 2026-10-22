"""OpenAI SDK client for OpenAI-compatible chat-completions providers."""

from __future__ import annotations

import logging
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Type

from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..errors import (
    ConfigError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRetriesExhausted,
    LLMTimeoutError,
)
from .parsing import extract_json_object

logger = logging.getLogger("sxsvn_intel.llm")


@dataclass
class LLMResponse:
    content: dict[str, Any]
    model_id: str
    prompt_version: str
    raw_text: str
    attempts: int
    usage: dict[str, Any] = field(default_factory=dict)


class LLMClient:
    """Small application wrapper around the official OpenAI Python SDK.

    ``base_url`` can point at any compatible chat-completions endpoint. SDK
    retries are disabled because this wrapper owns the configured retry budget
    and maps provider errors into the module's stable error hierarchy.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        logger_: logging.Logger | None = None,
    ) -> None:
        if not settings.llm_base_url:
            raise ConfigError("LLM base URL is not configured")
        self.settings = settings
        self._sleep = sleep
        self._logger = logger_ or logger
        self._response_format_disabled = False
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ConfigError(
                    "OpenAI SDK is not installed; install sxsvn-intel[llm]"
                ) from exc
            base_url = settings.llm_base_url.rstrip("/")
            if base_url.endswith("/chat/completions"):
                base_url = base_url[: -len("/chat/completions")]
            client = OpenAI(
                api_key=settings.llm_api_key or "not-required",
                base_url=base_url,
                timeout=settings.llm_timeout,
                max_retries=0,
            )
        self.client = client

    @property
    def model_id(self) -> str:
        return self.settings.llm_model

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_version: str,
        response_model: Type[BaseModel] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        attempts_total = self.settings.llm_max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts_total + 1):
            started = time.monotonic()
            request: dict[str, Any] = {
                "model": self.settings.llm_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": (
                    self.settings.llm_temperature if temperature is None else temperature
                ),
                "max_tokens": max_tokens or self.settings.llm_max_tokens,
            }
            if (
                self.settings.llm_response_format == "json_object"
                and not self._response_format_disabled
            ):
                request["response_format"] = {"type": "json_object"}

            try:
                response = self.client.chat.completions.create(**request)
            except Exception as exc:  # provider SDK exposes several exception types
                status = getattr(exc, "status_code", None)
                message = self._sanitize(str(exc))
                if status == 400 and "response_format" in request and not self._response_format_disabled and (
                    "response_format" in message.lower() or "json" in message.lower()
                ):
                    # Some compatible gateways reject this optional parameter.
                    self._response_format_disabled = True
                    last_error = LLMProviderError(
                        "Provider rejected response_format; retrying without it", status=400
                    )
                    self._log_attempt(
                        prompt_version, attempt, attempts_total, started, error="response_format"
                    )
                    continue
                if self._is_timeout(exc):
                    last_error = LLMTimeoutError(
                        f"LLM request timed out after {self.settings.llm_timeout}s"
                    )
                    outcome = "timeout"
                else:
                    last_error = LLMProviderError(
                        f"LLM provider request failed{f' (HTTP {status})' if status else ''}: "
                        f"{message[:500]}",
                        status=status if isinstance(status, int) else None,
                    )
                    outcome = f"http_{status}" if status else "transport"
                self._log_attempt(
                    prompt_version, attempt, attempts_total, started, error=outcome
                )
                if self._is_timeout(exc):
                    raise last_error from exc
                if status is not None and status not in {408, 409, 425, 429} and status < 500:
                    raise last_error from exc
                self._sleep(self._backoff(attempt))
                continue

            try:
                raw_text, content, usage, returned_model = self._read_response(
                    response, response_model
                )
            except LLMInvalidResponseError as exc:
                last_error = exc
                self._log_attempt(
                    prompt_version, attempt, attempts_total, started,
                    error=str(exc.details.get("finish_reason") or "invalid_json")
                )
                if exc.details.get("finish_reason") == "length":
                    raise
                if attempt < attempts_total:
                    self._sleep(self._backoff(attempt))
                continue

            self._log_attempt(prompt_version, attempt, attempts_total, started, ok=True)
            return LLMResponse(
                content=content,
                model_id=returned_model or self.settings.llm_model,
                prompt_version=prompt_version,
                raw_text=raw_text,
                attempts=attempt,
                usage=usage,
            )

        raise LLMRetriesExhausted(
            f"LLM request failed after {attempts_total} attempts",
            attempts=attempts_total,
            last_error=last_error,
        )

    def _read_response(
        self, response: Any, response_model: Type[BaseModel] | None
    ) -> tuple[str, dict[str, Any], dict[str, Any], str]:
        choices = getattr(response, "choices", None)
        if not choices:
            raise LLMInvalidResponseError("LLM response has no choices")
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "length":
            raise LLMInvalidResponseError(
                "LLM response was truncated because the output token limit was reached",
                details={"finish_reason": "length"},
            )
        message = getattr(choice, "message", None)
        raw = getattr(message, "content", None)
        if isinstance(raw, list):
            text_parts = []
            for part in raw:
                if isinstance(part, dict):
                    if part.get("type") in {"text", "output_text"}:
                        text_parts.append(str(part.get("text") or ""))
                elif getattr(part, "type", None) in {"text", "output_text"}:
                    text_parts.append(str(getattr(part, "text", "") or ""))
            raw = "".join(text_parts)
        if isinstance(raw, dict):
            raw_text = json.dumps(raw, ensure_ascii=False)
            payload = raw
        elif isinstance(raw, str) and raw.strip():
            raw_text = raw
            payload = extract_json_object(raw)
        else:
            raise LLMInvalidResponseError("LLM response has empty content")

        if response_model is not None:
            try:
                parsed = response_model.model_validate(payload)
            except ValidationError as exc:
                fields = [
                    ".".join(str(part) for part in error.get("loc", ()))
                    for error in exc.errors(include_input=False)
                ]
                raise LLMInvalidResponseError(
                    "LLM response does not match the expected schema"
                    + (f" (fields: {', '.join(fields[:8])})" if fields else "")
                ) from exc
            payload = parsed.model_dump(mode="json", exclude_none=True)

        usage_value = getattr(response, "usage", None)
        if hasattr(usage_value, "model_dump"):
            usage_value = usage_value.model_dump(mode="json")
        usage = usage_value if isinstance(usage_value, dict) else {}
        model = str(getattr(response, "model", "") or "")
        return raw_text, payload, usage, model

    def _sanitize(self, message: str) -> str:
        if self.settings.llm_api_key:
            message = message.replace(self.settings.llm_api_key, "***")
        return message[:800]

    @staticmethod
    def _is_timeout(exc: Exception) -> bool:
        return "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()

    def _backoff(self, attempt: int) -> float:
        delay = self.settings.llm_backoff_base * (2 ** (attempt - 1))
        return min(delay, self.settings.llm_backoff_max)

    def _log_attempt(
        self,
        prompt_version: str,
        attempt: int,
        total: int,
        started: float,
        *,
        ok: bool = False,
        error: str | None = None,
    ) -> None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._logger.info(
            "llm attempt %s/%s model=%s prompt=%s elapsed_ms=%s outcome=%s",
            attempt,
            total,
            self.settings.llm_model,
            prompt_version,
            elapsed_ms,
            "ok" if ok else (error or "retry"),
        )
