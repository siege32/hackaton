"""Explicit exception hierarchy for the AI news-intelligence module.

The hierarchy is intentionally small but distinguishes the situations the
calling system must react to differently: transport/LLM failures (retryable),
schema/grounding failures (retry or REVIEW), storage failures (must NOT ack a
queue job), and permanent data problems (dead-letter).
"""

from __future__ import annotations


class IntelError(Exception):
    """Base class for every error raised by the module."""

    code = "intel_error"

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message or self.__class__.__name__


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class ConfigError(IntelError):
    code = "config_error"


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
class LLMError(IntelError):
    code = "llm_error"


class LLMTimeoutError(LLMError):
    code = "llm_timeout"


class LLMProviderError(LLMError):
    code = "llm_provider_error"

    def __init__(self, message: str, *, status: int | None = None, details: dict | None = None) -> None:
        super().__init__(message, details=details)
        self.status = status


class LLMInvalidResponseError(LLMError):
    code = "llm_invalid_response"


class LLMRetriesExhausted(LLMError):
    code = "llm_retries_exhausted"

    def __init__(self, message: str, *, attempts: int, last_error: Exception | None = None) -> None:
        super().__init__(message, details={"attempts": attempts})
        self.attempts = attempts
        self.last_error = last_error


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class SchemaValidationError(IntelError):
    code = "schema_validation_error"

    def __init__(self, message: str, *, issues: list[str] | None = None) -> None:
        super().__init__(message, details={"issues": issues or []})
        self.issues = issues or []


class GroundingValidationError(SchemaValidationError):
    code = "grounding_validation_error"


class MissingEvidenceError(GroundingValidationError):
    code = "missing_evidence"


# ---------------------------------------------------------------------------
# Storage / queue
# ---------------------------------------------------------------------------
class StorageError(IntelError):
    code = "storage_error"


class QueueError(IntelError):
    code = "queue_error"


class QueueUnavailableError(QueueError):
    code = "queue_unavailable"


class MalformedJobError(QueueError):
    code = "malformed_job"

    def __init__(self, message: str, *, raw: str = "") -> None:
        super().__init__(message, details={"raw": raw[:500]})
        self.raw = raw


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
class NotFoundError(IntelError):
    code = "not_found"


class NewsNotFoundError(NotFoundError):
    code = "news_not_found"


class CompanyNotFoundError(NotFoundError):
    code = "company_not_found"


class AssessmentNotFoundError(NotFoundError):
    code = "assessment_not_found"


class AnalysisNotFoundError(NotFoundError):
    code = "analysis_not_found"


class NewsTextEmptyError(IntelError):
    code = "news_text_empty"


class StaleProcessingError(IntelError):
    code = "stale_processing"


class ProcessingNotReadyError(IntelError):
    code = "processing_not_ready"


# ---------------------------------------------------------------------------
# Tools / agent
# ---------------------------------------------------------------------------
class ToolError(IntelError):
    code = "tool_error"


class ToolUnavailableError(ToolError):
    code = "tool_unavailable"


class SearchLimitExceeded(ToolError):
    code = "search_limit_exceeded"
