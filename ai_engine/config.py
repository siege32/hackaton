from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .llm_constants import ALLOWED_MODELS, CHANGELLENGE_BASE_URL, DEFAULT_MODEL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_api_key: str = Field(default="", validation_alias="SXSVN_LLM_API_KEY")
    llm_base_url: str = Field(default=CHANGELLENGE_BASE_URL, validation_alias="SXSVN_LLM_BASE_URL")
    llm_model: str = Field(default=DEFAULT_MODEL, validation_alias="SXSVN_LLM_MODEL")
    llm_timeout: float = Field(default=30.0, validation_alias="SXSVN_LLM_TIMEOUT", gt=0)
    llm_max_retries: int = Field(default=2, validation_alias="SXSVN_LLM_MAX_RETRIES", ge=0, le=5)
    llm_max_input_chars: int = Field(default=40_000, validation_alias="SXSVN_LLM_MAX_INPUT_CHARS", gt=0)
    llm_max_output_tokens: int = Field(default=2_000, validation_alias="SXSVN_LLM_MAX_OUTPUT_TOKENS", gt=0)
    cache_ttl_seconds: int = Field(default=900, validation_alias="SXSVN_CACHE_TTL_SECONDS", ge=0)
    cache_max_size: int = Field(default=1024, validation_alias="SXSVN_CACHE_MAX_SIZE", gt=0)
    max_search_steps: int = Field(default=2, validation_alias="SXSVN_MAX_SEARCH_STEPS", ge=0, le=2)
    llm_retries: int = Field(default=2, validation_alias="SXSVN_LLM_RETRIES", ge=0, le=5)

    def validate_model(self) -> None:
        if self.llm_model not in ALLOWED_MODELS:
            raise ValueError(f"Unsupported model: {self.llm_model}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_model()
    return settings
