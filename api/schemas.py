from __future__ import annotations

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from ai_engine.models import AnalysisContext, BankEvidence, CompanyProfile, NewsItem, Source


class ApiContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NewsAnalysisRequest(ApiContract):
    event_id: str
    as_of_date: str
    profile: CompanyProfile
    news: NewsItem
    bank_evidence: tuple[BankEvidence, ...] = ()
    calculations: dict[str, object] = Field(default_factory=dict)

    def to_context(self) -> AnalysisContext:
        return AnalysisContext(
            company_id=self.profile.company_id,
            event_id=self.event_id,
            as_of_date=self.as_of_date,
            profile=self.profile,
            event_facts=(),
            bank_evidence=self.bank_evidence,
            calculations=self.calculations,
            news=self.news,
            sources=(
                Source(
                    source_id=self.news.source_id,
                    title=self.news.title,
                    url=self.news.url,
                    published_at=self.news.published_at,
                    kind="news",
                ),
            ),
        )


class NewsParseRequest(ApiContract):
    source_url: AnyHttpUrl | None = None
    raw_text: str | None = None
    title: str = ""
    published_at: str | None = None
    language: str = "ru"

    @model_validator(mode="after")
    def require_one_input(self):
        if (self.source_url is None) == (self.raw_text is None):
            raise ValueError("provide exactly one of source_url or raw_text")
        if self.raw_text is not None and not self.raw_text.strip():
            raise ValueError("raw_text must not be empty")
        return self


class ParsedNewsResponse(ApiContract):
    news: NewsItem
    source: Source
    duplicate: bool = False