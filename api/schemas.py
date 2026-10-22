from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


class ApiContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnalysisRequest(ApiContract):
    company_id: str = Field(min_length=1, max_length=120)
    event_ids: list[str] = Field(min_length=1, max_length=20)


class NewsCreateRequest(ApiContract):
    title: str = Field(min_length=2, max_length=500)
    text: str = Field(min_length=20, max_length=100_000)
    url: AnyHttpUrl | None = None
    published_at: str | None = None

    @field_validator("title", "text")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("поле не должно быть пустым")
        return value


class NewsFetchRequest(ApiContract):
    source_ids: list[str] = Field(default_factory=list, max_length=8)
    max_items: int | None = Field(default=None, ge=1, le=100)


class RefinementRequest(ApiContract):
    text: str = Field(min_length=3, max_length=4000)
    kind: Literal[
        "business_model", "dependency", "product", "region", "horizon", "not_relevant", "other"
    ] = "other"
    affected_event_ids: list[str] = Field(default_factory=list, max_length=20)
    horizon: Literal["short_term", "medium_term", "long_term", "unknown"] | None = None

    @field_validator("text")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("уточнение не должно быть пустым")
        return value
