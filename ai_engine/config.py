"""Configuration for the AI news-intelligence module.

All values are read from environment variables so that the module can be
wired into the existing project configuration (pydantic-settings, dotenv,
Docker env, etc.) without code changes.

The API key is never stored in a representation of the settings and is never
logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Mapping

from .errors import ConfigError

#: Models made available by the challenge provider.  The default is a
#: *preliminary* choice: no comparative benchmark of these models has been run
#: in this module, so it must not be presented as a measured optimum.
KNOWN_MODELS = (
    "google/gemma-4-26b-a4b-it",
    "z-ai/glm-5.3-flash",
    "qwen/qwen3.6-35b-a3b",
    "deepseek/deepseek-v4.1-flash",
    "qwen/qwen3-vl-30b-a3b-instruct",
    "moonshotai/kimi-k2.6",
)

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"


def _env(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    value = env.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value != "" else default


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _env(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _env(env, name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _env(env, name)
    if raw is None:
        return default
    value = raw.lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration."""

    # --- LLM -------------------------------------------------------------
    llm_base_url: str = "https://changellenge.up.railway.app/v1"
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = DEFAULT_MODEL
    llm_timeout: float = 180.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.0
    llm_max_tokens: int = 8000
    llm_max_input_chars: int = 24000
    #: ``json_object`` | ``json_schema`` | ``none``.  ``json_object`` is the
    #: most widely supported structured-output mode of OpenAI-compatible APIs.
    llm_response_format: str = "json_object"
    llm_backoff_base: float = 0.5
    llm_backoff_max: float = 8.0
    #: Escape hatch for using a model outside KNOWN_MODELS (e.g. a local mock).
    llm_allow_any_model: bool = False
    #: ``llm`` | ``rules`` | ``auto``.  ``auto`` uses the LLM when a key is
    #: configured, otherwise the transparent rule-based fallback.
    classifier_mode: str = "auto"

    # --- Redis queue -----------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    queue_key: str = "sxsvn:news:queue"
    processing_key: str = "sxsvn:news:processing"
    dead_letter_key: str = "sxsvn:news:dead_letter"
    job_meta_key: str = "sxsvn:news:job_meta"
    retry_delay_key: str = "sxsvn:news:delayed"
    worker_visibility_timeout: float = 900.0
    worker_max_attempts: int = 3
    worker_retry_delay: float = 5.0
    worker_block_timeout: float = 5.0

    # --- Processing ------------------------------------------------------
    processing_version: str = "v1"
    max_additional_searches: int = 2
    max_relationships: int = 50
    max_facts_per_prompt: int = 40

    # --- Storage ---------------------------------------------------------
    #: When set, ``build_runtime`` opens this SQLite database and creates the
    #: additive ``intel_*`` tables.  Otherwise only explicit adapters are used.
    sqlite_path: str | None = None
    #: News table adapter used with the SQL store (owned by the news team).
    news_table: str = "news"
    company_table: str = "companies"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = env if env is not None else os.environ
        model = _env(env, "LLM_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
        allow_any = _env_bool(env, "LLM_ALLOW_ANY_MODEL", False)
        if not allow_any and model not in KNOWN_MODELS:
            raise ConfigError(
                f"LLM_MODEL={model!r} is not in the allowed model list. "
                "Set LLM_ALLOW_ANY_MODEL=1 only for local/mock providers."
            )
        mode = (_env(env, "CLASSIFIER_MODE", "auto") or "auto").lower()
        if mode not in {"llm", "rules", "auto"}:
            raise ConfigError("CLASSIFIER_MODE must be one of: llm, rules, auto")
        response_format = (_env(env, "LLM_RESPONSE_FORMAT", "json_object") or "json_object").lower()
        if response_format not in {"json_object", "json_schema", "none"}:
            raise ConfigError("LLM_RESPONSE_FORMAT must be json_object, json_schema or none")
        settings = cls(
            llm_base_url=(_env(env, "LLM_BASE_URL", cls.llm_base_url) or "").rstrip("/"),
            llm_api_key=_env(env, "LLM_API_KEY", "") or "",
            llm_model=model,
            llm_timeout=_env_float(env, "LLM_TIMEOUT", 180.0),
            llm_max_retries=_env_int(env, "LLM_MAX_RETRIES", 2),
            llm_temperature=_env_float(env, "LLM_TEMPERATURE", 0.0),
            llm_max_tokens=_env_int(env, "LLM_MAX_TOKENS", 8000),
            llm_max_input_chars=_env_int(env, "LLM_MAX_INPUT_CHARS", 24000),
            llm_response_format=response_format,
            llm_backoff_base=_env_float(env, "LLM_BACKOFF_BASE", 0.5),
            llm_backoff_max=_env_float(env, "LLM_BACKOFF_MAX", 8.0),
            llm_allow_any_model=allow_any,
            classifier_mode=mode,
            redis_url=_env(env, "REDIS_URL", "redis://localhost:6379/0") or "",
            queue_key=_env(env, "NEWS_QUEUE_KEY", "sxsvn:news:queue") or "",
            processing_key=_env(env, "NEWS_PROCESSING_KEY", "sxsvn:news:processing") or "",
            dead_letter_key=_env(env, "NEWS_DEAD_LETTER_KEY", "sxsvn:news:dead_letter") or "",
            job_meta_key=_env(env, "NEWS_JOB_META_KEY", "sxsvn:news:job_meta") or "",
            retry_delay_key=_env(env, "NEWS_RETRY_DELAY_KEY", "sxsvn:news:delayed") or "",
            worker_visibility_timeout=_env_float(env, "WORKER_VISIBILITY_TIMEOUT", 900.0),
            worker_max_attempts=_env_int(env, "WORKER_MAX_ATTEMPTS", 3),
            worker_retry_delay=_env_float(env, "WORKER_RETRY_DELAY", 5.0),
            worker_block_timeout=_env_float(env, "WORKER_BLOCK_TIMEOUT", 5.0),
            processing_version=_env(env, "PROCESSING_VERSION", "v1") or "v1",
            max_additional_searches=_env_int(env, "INSIGHT_MAX_ADDITIONAL_SEARCHES", 2),
            max_relationships=_env_int(env, "INSIGHT_MAX_RELATIONSHIPS", 50),
            max_facts_per_prompt=_env_int(env, "INSIGHT_MAX_FACTS", 40),
            sqlite_path=_env(env, "INTEL_SQLITE_PATH"),
            news_table=_env(env, "INTEL_NEWS_TABLE", "news") or "news",
            company_table=_env(env, "INTEL_COMPANY_TABLE", "companies") or "companies",
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.llm_base_url:
            raise ConfigError("LLM_BASE_URL must not be empty")
        if self.llm_timeout <= 0:
            raise ConfigError("LLM_TIMEOUT must be positive")
        if self.llm_max_retries < 0:
            raise ConfigError("LLM_MAX_RETRIES must be >= 0")
        if not (0.0 <= self.llm_temperature <= 2.0):
            raise ConfigError("LLM_TEMPERATURE must be between 0 and 2")
        if self.llm_max_input_chars < 1000:
            raise ConfigError("LLM_MAX_INPUT_CHARS must be at least 1000")
        if not (0 <= self.max_additional_searches <= 2):
            # The product requirement is "max two searches"; a configuration
            # must not be able to silently lift it.
            raise ConfigError("INSIGHT_MAX_ADDITIONAL_SEARCHES must be between 0 and 2")
        if self.worker_max_attempts < 1:
            raise ConfigError("WORKER_MAX_ATTEMPTS must be >= 1")

    @property
    def use_llm(self) -> bool:
        if self.classifier_mode == "rules":
            return False
        if self.classifier_mode == "llm":
            return True
        return bool(self.llm_api_key)

    def with_overrides(self, **kwargs) -> "Settings":
        updated = replace(self, **kwargs)
        updated.validate()
        return updated

    def public_dict(self) -> dict:
        """Safe representation: never includes the API key."""
        data = {
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_timeout": self.llm_timeout,
            "llm_max_retries": self.llm_max_retries,
            "llm_temperature": self.llm_temperature,
            "llm_response_format": self.llm_response_format,
            "llm_api_key_configured": bool(self.llm_api_key),
            "classifier_mode": self.classifier_mode,
            "queue_key": self.queue_key,
            "processing_key": self.processing_key,
            "dead_letter_key": self.dead_letter_key,
            "worker_max_attempts": self.worker_max_attempts,
            "processing_version": self.processing_version,
            "max_additional_searches": self.max_additional_searches,
        }
        return data

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Settings({self.public_dict()})"
