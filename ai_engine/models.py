from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Decision = Literal["KEEP", "DROP", "REVIEW"]
Status = Literal["completed", "needs_review", "failed"]
ToolStatus = Literal["success", "empty", "partial", "error"]
Channel = Literal["direct", "counterparty", "industry", "geography", "none"]
EvidenceKind = Literal["event", "bank", "calculation", "hypothesis"]


class ContractModel(BaseModel):
    """Base for every public contract: immutable and strict about extra keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(ContractModel):
    source_id: str
    title: str = ""
    url: str | None = None
    published_at: str | None = None
    kind: Literal["news", "bank", "registry", "calculation", "other"] = "other"


class EventFact(ContractModel):
    fact_id: str
    text: str
    source_ids: tuple[str, ...]
    verified: bool = True
    value: Any = None
    topic: str = ""
    occurred_at: str | None = None
    published_at: str | None = None
    participants: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    geographies: tuple[str, ...] = ()
    unconfirmed: tuple[str, ...] = ()
    quote: str | None = None


class EventMeta(ContractModel):
    """Event-level metadata. ``occurred_at`` stays empty when the date is unknown."""

    event_id: str = ""
    topic: str = ""
    occurred_at: str | None = None
    published_at: str | None = None
    participants: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    geographies: tuple[str, ...] = ()
    unconfirmed: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()


class EventExtraction(EventMeta):
    facts: tuple[EventFact, ...] = ()
    extractor: Literal["provided", "heuristic", "llm"] = "provided"


class NewsItem(ContractModel):
    news_id: str
    text: str
    source_id: str
    title: str = ""
    url: str | None = None
    published_at: str | None = None
    language: str = "ru"


class BankEvidence(ContractModel):
    """Bank-side observation supplied by an external integration.

    Extra fields are allowed so upstream systems can add attributes without a
    contract change; the core fields are what the engine relies on.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    source_id: str
    text: str
    verified: bool = True
    value: Any = None


class BankReport(ContractModel):
    document_id: str
    title: str
    excerpt: str
    topics: tuple[str, ...] = ()
    published_at: str | None = None
    sensitivity: Literal["public", "internal", "other"] = "internal"


class CompanyProfile(ContractModel):
    company_id: str
    name: str
    industries: tuple[str, ...] = ()
    geographies: tuple[str, ...] = ()
    counterparties: tuple[str, ...] = ()
    features: dict[str, Any] = Field(default_factory=dict)


class AnalysisContext(ContractModel):
    company_id: str
    event_id: str
    as_of_date: str
    profile: CompanyProfile
    event_facts: tuple[EventFact, ...]
    bank_evidence: tuple[BankEvidence, ...] = ()
    calculations: dict[str, Any] = Field(default_factory=dict)
    claims: tuple[dict[str, Any], ...] = ()
    missing_data: tuple[str, ...] = ()
    tool_trace: tuple[dict[str, Any], ...] = ()
    retry_count: int = 0
    status: str = "ready"
    schema_version: str = "1.0"
    run_id: str = ""
    event: EventMeta | None = None
    sources: tuple[Source, ...] = ()
    news: NewsItem | None = None
    clarification: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Evidence(ContractModel):
    evidence_id: str
    source_id: str
    quote: str
    kind: EvidenceKind
    fact_id: str | None = None


class Claim(ContractModel):
    claim_id: str
    text: str
    evidence_ids: tuple[str, ...]
    limitation: str = ""
    claim_type: Literal["fact", "hypothesis", "implication"] = "hypothesis"
    feature: str = ""
    mechanism: str = ""
    consequence: str = ""
    revision_note: str = ""


class RelevanceAssessment(ContractModel):
    decision: Decision
    score: float = Field(ge=0.0, le=1.0)
    rationale: str
    bases: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    channel: Channel = "none"


class TraceStep(ContractModel):
    step: int
    action: str
    status: str = "ok"
    tool: str | None = None
    detail: str = ""


class ToolResult(ContractModel):
    tool: str
    status: ToolStatus
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    origin: Literal["context", "external", "calculation"] = "external"

    def succeeded(self) -> bool:
        return self.status in {"success", "partial"}


class AnalysisResult(ContractModel):
    event: dict[str, Any]
    priority: dict[str, Any]
    company_features: dict[str, Any]
    bank_view: dict[str, Any]
    ai_implications: tuple[Claim, ...]
    risks: tuple[Claim, ...]
    opportunities: tuple[Claim, ...]
    missing_data: tuple[str, ...]
    next_check: str | None
    evidence: tuple[Evidence, ...]
    trace_id: str
    status: Status
    relevance: dict[str, Any] = Field(default_factory=dict)
    evidence_quality: dict[str, Any] = Field(default_factory=dict)
    trace: tuple[TraceStep, ...] = ()
    clarification_applied: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
