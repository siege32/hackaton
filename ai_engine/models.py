"""Domain models and enums for the news-intelligence module.

Design rules encoded here:

* processing status (technical) is independent from business relevance
  (``KEEP``/``DROP``/``REVIEW``);
* general news facts never contain a company assessment;
* a company assessment always carries ``company_id`` and profile/version
  metadata used to produce it;
* direction, type and horizon of impact are separate fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .utils import content_version as make_content_version
from .utils import jsonable, stable_id, utcnow_iso


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class RelevanceClass(StrEnum):
    KEEP = "KEEP"
    DROP = "DROP"
    REVIEW = "REVIEW"


class ImpactDirection(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ImpactType(StrEnum):
    DIRECT = "direct"
    INDIRECT = "indirect"
    UNKNOWN = "unknown"


class ImpactHorizon(StrEnum):
    SHORT_TERM = "short_term"
    MEDIUM_TERM = "medium_term"
    LONG_TERM = "long_term"
    UNKNOWN = "unknown"


class AnalysisStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class FactOrigin(StrEnum):
    """Whether a fact is stated by the source or inferred by the model."""

    SOURCE = "source"
    MODEL_INFERENCE = "model_inference"


class Certainty(StrEnum):
    ASSERTED = "asserted"
    UNCERTAIN = "uncertain"


class RelationType(StrEnum):
    SUPPLIER = "supplier"
    CUSTOMER = "customer"
    PARTNER = "partner"
    CREDITOR = "creditor"
    DEBTOR = "debtor"
    PAYMENT_LINK = "payment_link"
    OTHER = "other"
    UNKNOWN = "unknown"


class Significance(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class RefinementKind(StrEnum):
    BUSINESS_MODEL = "business_model"
    DEPENDENCY = "dependency"
    PRODUCT = "product"
    REGION = "region"
    HORIZON = "horizon"
    NOT_RELEVANT = "not_relevant"
    OTHER = "other"


def _enum(cls, value: Any, default: Any = None):
    if value is None:
        return default
    try:
        return cls(str(value))
    except ValueError:
        return default


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_str_list(value: Any) -> list[str]:
    return [str(v) for v in _as_list(value) if v is not None and str(v).strip() != ""]


# ---------------------------------------------------------------------------
# News input
# ---------------------------------------------------------------------------
@dataclass
class NewsRecord:
    news_id: str
    title: str
    text: str
    source_id: str = ""
    content_version: str = ""
    published_at: str | None = None
    url: str | None = None
    language: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content_version:
            self.content_version = make_content_version(self.news_id, self.title, self.text)

    @property
    def full_text(self) -> str:
        if self.title and self.title not in self.text:
            return f"{self.title}\n\n{self.text}".strip()
        return self.text.strip()

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NewsRecord":
        return cls(
            news_id=str(data.get("news_id") or data.get("id") or ""),
            title=str(data.get("title") or ""),
            text=str(data.get("text") or data.get("body") or ""),
            source_id=str(data.get("source_id") or data.get("source") or ""),
            content_version=str(data.get("content_version") or ""),
            published_at=data.get("published_at"),
            url=data.get("url"),
            language=data.get("language"),
            metadata=dict(data.get("metadata") or {}),
        )


# ---------------------------------------------------------------------------
# Facts (stage A, company-independent)
# ---------------------------------------------------------------------------
@dataclass
class Quantity:
    value: float | None
    unit: str | None
    raw: str

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Quantity":
        value = data.get("value")
        try:
            numeric = float(value) if value is not None and value != "" else None
        except (TypeError, ValueError):
            numeric = None
        return cls(value=numeric, unit=(data.get("unit") or None), raw=str(data.get("raw") or ""))


@dataclass
class NewsFact:
    fact_id: str
    news_id: str
    content_version: str
    fact_type: str
    subject: str
    action: str
    quote: str
    source_id: str = ""
    object: str | None = None
    date: str | None = None
    period: str | None = None
    quantities: list[Quantity] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    negated: bool = False
    origin: FactOrigin = FactOrigin.SOURCE
    certainty: Certainty = Certainty.ASSERTED
    related_fact_ids: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NewsFact":
        return cls(
            fact_id=str(data.get("fact_id") or ""),
            news_id=str(data.get("news_id") or ""),
            content_version=str(data.get("content_version") or ""),
            fact_type=str(data.get("fact_type") or "other"),
            subject=str(data.get("subject") or ""),
            action=str(data.get("action") or ""),
            quote=str(data.get("quote") or ""),
            source_id=str(data.get("source_id") or ""),
            object=data.get("object"),
            date=data.get("date"),
            period=data.get("period"),
            quantities=[Quantity.from_dict(q) for q in _as_list(data.get("quantities")) if isinstance(q, dict)],
            conditions=_as_str_list(data.get("conditions")),
            negated=bool(data.get("negated", False)),
            origin=_enum(FactOrigin, data.get("origin"), FactOrigin.SOURCE),
            certainty=_enum(Certainty, data.get("certainty"), Certainty.ASSERTED),
            related_fact_ids=_as_str_list(data.get("related_fact_ids")),
            extra=dict(data.get("extra") or {}),
            created_at=str(data.get("created_at") or utcnow_iso()),
        )

    def stable_key(self) -> tuple:
        return (
            self.news_id,
            self.content_version,
            self.fact_type,
            self.subject.strip().lower(),
            self.action.strip().lower(),
            (self.object or "").strip().lower(),
            self.quote.strip().lower(),
            tuple(sorted(q.raw for q in self.quantities)),
            self.negated,
        )

    def assign_stable_id(self) -> "NewsFact":
        self.fact_id = stable_id("fact", *self.stable_key())
        return self


@dataclass
class NewsEntities:
    companies: list[str] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    geographies: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NewsEntities":
        return cls(
            companies=_as_str_list(data.get("companies")),
            organizations=_as_str_list(data.get("organizations")),
            people=_as_str_list(data.get("people")),
            products=_as_str_list(data.get("products")),
            resources=_as_str_list(data.get("resources")),
            dates=_as_str_list(data.get("dates")),
            geographies=_as_str_list(data.get("geographies")),
            event_types=_as_str_list(data.get("event_types")),
        )


@dataclass
class NewsProcessingResult:
    news_id: str
    content_version: str
    status: ProcessingStatus
    general_relevance: RelevanceClass = RelevanceClass.REVIEW
    classification_reason: str = ""
    event_type: str = ""
    main_events: list[str] = field(default_factory=list)
    entities: NewsEntities = field(default_factory=NewsEntities)
    facts: list[NewsFact] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    source_id: str = ""
    model_id: str = ""
    prompt_version: str = ""
    processing_version: str = ""
    job_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    @property
    def processed(self) -> bool:
        return self.status == ProcessingStatus.PROCESSED

    @property
    def fact_ids(self) -> list[str]:
        return [f.fact_id for f in self.facts]

    def fact_by_id(self, fact_id: str) -> NewsFact | None:
        return next((f for f in self.facts if f.fact_id == fact_id), None)

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NewsProcessingResult":
        entities = data.get("entities")
        return cls(
            news_id=str(data.get("news_id") or ""),
            content_version=str(data.get("content_version") or ""),
            status=_enum(ProcessingStatus, data.get("status"), ProcessingStatus.PENDING),
            general_relevance=_enum(RelevanceClass, data.get("general_relevance"), RelevanceClass.REVIEW),
            classification_reason=str(data.get("classification_reason") or ""),
            event_type=str(data.get("event_type") or ""),
            main_events=_as_str_list(data.get("main_events")),
            entities=NewsEntities.from_dict(entities) if isinstance(entities, dict) else NewsEntities(),
            facts=[NewsFact.from_dict(f) for f in _as_list(data.get("facts")) if isinstance(f, dict)],
            uncertainties=_as_str_list(data.get("uncertainties")),
            contradictions=_as_str_list(data.get("contradictions")),
            missing_data=_as_str_list(data.get("missing_data")),
            validation_warnings=_as_str_list(data.get("validation_warnings")),
            source_id=str(data.get("source_id") or ""),
            model_id=str(data.get("model_id") or ""),
            prompt_version=str(data.get("prompt_version") or ""),
            processing_version=str(data.get("processing_version") or ""),
            job_id=data.get("job_id"),
            error_code=data.get("error_code"),
            error_message=data.get("error_message"),
            created_at=str(data.get("created_at") or utcnow_iso()),
            updated_at=str(data.get("updated_at") or utcnow_iso()),
        )


# ---------------------------------------------------------------------------
# Company context
# ---------------------------------------------------------------------------
@dataclass
class CompanyProfile:
    company_id: str
    name: str
    industry: str | None = None
    subindustry: str | None = None
    activities: list[str] = field(default_factory=list)
    products_services: list[str] = field(default_factory=list)
    processes: list[str] = field(default_factory=list)
    key_resources: list[str] = field(default_factory=list)
    cost_drivers: list[str] = field(default_factory=list)
    geography: list[str] = field(default_factory=list)
    suppliers: list[dict[str, Any]] = field(default_factory=list)
    customers: list[dict[str, Any]] = field(default_factory=list)
    financials: dict[str, Any] = field(default_factory=dict)
    known_risks: list[str] = field(default_factory=list)
    data_sources: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str | None = None
    profile_version: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.profile_version:
            payload = jsonable(self)
            payload.pop("profile_version", None)
            self.profile_version = make_content_version(self.company_id, payload)

    def has_minimal_context(self) -> bool:
        """Whether the profile contains anything beyond a name/industry."""
        return bool(
            self.activities
            or self.products_services
            or self.processes
            or self.key_resources
            or self.cost_drivers
            or self.geography
            or self.suppliers
            or self.customers
            or self.financials
            or self.known_risks
        )

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CompanyProfile":
        return cls(
            company_id=str(data.get("company_id") or data.get("id") or ""),
            name=str(data.get("name") or ""),
            industry=data.get("industry"),
            subindustry=data.get("subindustry"),
            activities=_as_str_list(data.get("activities")),
            products_services=_as_str_list(data.get("products_services")),
            processes=_as_str_list(data.get("processes")),
            key_resources=_as_str_list(data.get("key_resources")),
            cost_drivers=_as_str_list(data.get("cost_drivers")),
            geography=_as_str_list(data.get("geography")),
            suppliers=[s for s in _as_list(data.get("suppliers")) if isinstance(s, dict)],
            customers=[c for c in _as_list(data.get("customers")) if isinstance(c, dict)],
            financials=dict(data.get("financials") or {}),
            known_risks=_as_str_list(data.get("known_risks")),
            data_sources=[s for s in _as_list(data.get("data_sources")) if isinstance(s, dict)],
            updated_at=data.get("updated_at"),
            profile_version=str(data.get("profile_version") or ""),
            raw=dict(data.get("raw") or {}),
        )


@dataclass
class Relationship:
    relationship_id: str
    company_id: str
    counterparty_id: str
    counterparty_name: str
    relation_type: RelationType = RelationType.UNKNOWN
    confirmed: bool = False
    significance: Significance = Significance.UNKNOWN
    direction: str = "unknown"
    share: float | None = None
    evidence: list[str] = field(default_factory=list)
    valid_from: str | None = None
    valid_until: str | None = None
    source: str = ""
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_payment_artifact(self) -> bool:
        return self.relation_type in {RelationType.PAYMENT_LINK}

    def dependency_evidence(self) -> str:
        """Classify how much this edge supports a dependency claim.

        A payment link or a single payment proves a transaction, not a
        long-term dependency.  Unconfirmed edges are hypotheses only.
        """
        if self.is_payment_artifact or self.relation_type in {RelationType.UNKNOWN, RelationType.OTHER}:
            return "none"
        if not self.confirmed:
            return "hypothesis"
        if self.relation_type in {RelationType.SUPPLIER, RelationType.CUSTOMER}:
            if self.significance in {Significance.HIGH, Significance.MEDIUM}:
                return "confirmed"
            return "hypothesis"
        return "confirmed"

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Relationship":
        return cls(
            relationship_id=str(data.get("relationship_id") or ""),
            company_id=str(data.get("company_id") or ""),
            counterparty_id=str(data.get("counterparty_id") or data.get("counterparty_name") or ""),
            counterparty_name=str(data.get("counterparty_name") or data.get("name") or ""),
            relation_type=_enum(RelationType, data.get("relation_type"), RelationType.UNKNOWN),
            confirmed=bool(data.get("confirmed", False)),
            significance=_enum(Significance, data.get("significance"), Significance.UNKNOWN),
            direction=str(data.get("direction") or "unknown"),
            share=data.get("share"),
            evidence=_as_str_list(data.get("evidence")),
            valid_from=data.get("valid_from"),
            valid_until=data.get("valid_until"),
            source=str(data.get("source") or ""),
            updated_at=data.get("updated_at"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class ExposureMatch:
    """A concrete link between an event and a company-profile field."""

    kind: str  # revenue | cost | supply | demand | operations | risk
    field: str
    matched_value: str
    matched_term: str
    evidence: str
    relationship_id: str | None = None

    def to_dict(self) -> dict:
        return jsonable(self)


@dataclass
class CompanyContext:
    profile: CompanyProfile
    relationships: list[Relationship] = field(default_factory=list)
    exposures: list[ExposureMatch] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    news_facts: list[NewsFact] = field(default_factory=list)
    news_text: str = ""

    def to_dict(self) -> dict:
        return jsonable(self)


# ---------------------------------------------------------------------------
# Company-specific assessment (stage B)
# ---------------------------------------------------------------------------
@dataclass
class EvidenceItem:
    fact_id: str
    source_id: str
    quote: str
    supports: str

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceItem":
        return cls(
            fact_id=str(data.get("fact_id") or ""),
            source_id=str(data.get("source_id") or ""),
            quote=str(data.get("quote") or ""),
            supports=str(data.get("supports") or ""),
        )


@dataclass
class CompanyNewsAssessment:
    news_id: str
    company_id: str
    relevance_class: RelevanceClass
    impact_direction: ImpactDirection = ImpactDirection.UNKNOWN
    impact_type: ImpactType = ImpactType.UNKNOWN
    impact_mechanism: str = ""
    impact_horizon: ImpactHorizon = ImpactHorizon.UNKNOWN
    confidence: float | None = None
    reasoning_summary: str = ""
    supporting_fact_ids: list[str] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    # --- reproducibility / idempotency --------------------------------
    content_version: str = ""
    profile_version: str = ""
    model_id: str = ""
    prompt_version: str = ""
    processing_version: str = ""
    classification_source: str = "llm"  # llm | rules | llm_rejected | user_refinement
    user_statement: bool = False
    assessment_id: str = ""
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    def __post_init__(self) -> None:
        # Accept plain strings from adapters/DB and coerce them to enums.
        if not isinstance(self.relevance_class, RelevanceClass):
            self.relevance_class = RelevanceClass(str(self.relevance_class))
        if not isinstance(self.impact_direction, ImpactDirection):
            self.impact_direction = ImpactDirection(str(self.impact_direction))
        if not isinstance(self.impact_type, ImpactType):
            self.impact_type = ImpactType(str(self.impact_type))
        if not isinstance(self.impact_horizon, ImpactHorizon):
            self.impact_horizon = ImpactHorizon(str(self.impact_horizon))
        if not self.assessment_id:
            self.assessment_id = stable_id("assess", self.news_id, self.company_id, self.content_version, self.profile_version)
        if self.confidence is not None:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CompanyNewsAssessment":
        return cls(
            news_id=str(data.get("news_id") or ""),
            company_id=str(data.get("company_id") or ""),
            relevance_class=_enum(RelevanceClass, data.get("relevance_class"), RelevanceClass.REVIEW),
            impact_direction=_enum(ImpactDirection, data.get("impact_direction"), ImpactDirection.UNKNOWN),
            impact_type=_enum(ImpactType, data.get("impact_type"), ImpactType.UNKNOWN),
            impact_mechanism=str(data.get("impact_mechanism") or ""),
            impact_horizon=_enum(ImpactHorizon, data.get("impact_horizon"), ImpactHorizon.UNKNOWN),
            confidence=data.get("confidence"),
            reasoning_summary=str(data.get("reasoning_summary") or ""),
            supporting_fact_ids=_as_str_list(data.get("supporting_fact_ids")),
            evidence=[EvidenceItem.from_dict(e) for e in _as_list(data.get("evidence")) if isinstance(e, dict)],
            limitations=_as_str_list(data.get("limitations")),
            missing_data=_as_str_list(data.get("missing_data")),
            content_version=str(data.get("content_version") or ""),
            profile_version=str(data.get("profile_version") or ""),
            model_id=str(data.get("model_id") or ""),
            prompt_version=str(data.get("prompt_version") or ""),
            processing_version=str(data.get("processing_version") or ""),
            classification_source=str(data.get("classification_source") or "llm"),
            user_statement=bool(data.get("user_statement", False)),
            assessment_id=str(data.get("assessment_id") or ""),
            created_at=str(data.get("created_at") or utcnow_iso()),
            updated_at=str(data.get("updated_at") or utcnow_iso()),
        )


# ---------------------------------------------------------------------------
# Insight engine
# ---------------------------------------------------------------------------
@dataclass
class EvidenceLink:
    kind: str  # fact | relationship | analytics | calculation | inference | hypothesis | user_statement
    ref_id: str
    quote: str = ""
    source_id: str = ""
    supports: str = ""
    confirmed: bool = True

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceLink":
        return cls(
            kind=str(data.get("kind") or "inference"),
            ref_id=str(data.get("ref_id") or ""),
            quote=str(data.get("quote") or ""),
            source_id=str(data.get("source_id") or ""),
            supports=str(data.get("supports") or ""),
            confirmed=bool(data.get("confirmed", True)),
        )


@dataclass
class ImpactEvent:
    news_id: str
    relevance_class: RelevanceClass = RelevanceClass.REVIEW
    impact_direction: ImpactDirection = ImpactDirection.UNKNOWN
    impact_type: ImpactType = ImpactType.UNKNOWN
    mechanism: str = ""
    potential_consequences: list[str] = field(default_factory=list)
    evidence: list[EvidenceLink] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    confidence: float | None = None
    impact_horizon: ImpactHorizon = ImpactHorizon.UNKNOWN
    chain: list[str] = field(default_factory=list)
    hypothesis: bool = False

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ImpactEvent":
        return cls(
            news_id=str(data.get("news_id") or ""),
            relevance_class=_enum(RelevanceClass, data.get("relevance_class"), RelevanceClass.REVIEW),
            impact_direction=_enum(ImpactDirection, data.get("impact_direction"), ImpactDirection.UNKNOWN),
            impact_type=_enum(ImpactType, data.get("impact_type"), ImpactType.UNKNOWN),
            mechanism=str(data.get("mechanism") or ""),
            potential_consequences=_as_str_list(data.get("potential_consequences")),
            evidence=[EvidenceLink.from_dict(e) for e in _as_list(data.get("evidence")) if isinstance(e, dict)],
            limitations=_as_str_list(data.get("limitations")),
            confidence=data.get("confidence"),
            impact_horizon=_enum(ImpactHorizon, data.get("impact_horizon"), ImpactHorizon.UNKNOWN),
            chain=_as_str_list(data.get("chain")),
            hypothesis=bool(data.get("hypothesis", False)),
        )


@dataclass
class InsightResult:
    company_id: str
    analysis_status: AnalysisStatus
    summary: str = ""
    events: list[ImpactEvent] = field(default_factory=list)
    key_risks: list[str] = field(default_factory=list)
    potential_opportunities: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    recommended_checks: list[str] = field(default_factory=list)
    sources_used: list[dict[str, Any]] = field(default_factory=list)
    additional_searches_used: int = 0
    limitations: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    analysis_id: str = ""
    company_profile_version: str = ""
    parent_analysis_id: str | None = None
    refinements: list[dict[str, Any]] = field(default_factory=list)
    changed_event_ids: list[str] = field(default_factory=list)
    model_id: str = ""
    prompt_version: str = ""
    created_at: str = field(default_factory=utcnow_iso)

    def __post_init__(self) -> None:
        if not self.analysis_id:
            self.analysis_id = stable_id(
                "analysis",
                self.company_id,
                self.company_profile_version,
                ",".join(e.news_id for e in self.events),
                self.created_at,
            )

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "InsightResult":
        return cls(
            company_id=str(data.get("company_id") or ""),
            analysis_status=_enum(AnalysisStatus, data.get("analysis_status"), AnalysisStatus.PARTIAL),
            summary=str(data.get("summary") or ""),
            events=[ImpactEvent.from_dict(e) for e in _as_list(data.get("events")) if isinstance(e, dict)],
            key_risks=_as_str_list(data.get("key_risks")),
            potential_opportunities=_as_str_list(data.get("potential_opportunities")),
            unknowns=_as_str_list(data.get("unknowns")),
            recommended_checks=_as_str_list(data.get("recommended_checks")),
            sources_used=[s for s in _as_list(data.get("sources_used")) if isinstance(s, dict)],
            additional_searches_used=int(data.get("additional_searches_used") or 0),
            limitations=_as_str_list(data.get("limitations")),
            missing_data=_as_str_list(data.get("missing_data")),
            analysis_id=str(data.get("analysis_id") or ""),
            company_profile_version=str(data.get("company_profile_version") or ""),
            parent_analysis_id=data.get("parent_analysis_id"),
            refinements=[r for r in _as_list(data.get("refinements")) if isinstance(r, dict)],
            changed_event_ids=_as_str_list(data.get("changed_event_ids")),
            model_id=str(data.get("model_id") or ""),
            prompt_version=str(data.get("prompt_version") or ""),
            created_at=str(data.get("created_at") or utcnow_iso()),
        )


@dataclass
class Refinement:
    refinement_id: str
    company_id: str
    analysis_id: str
    text: str
    kind: RefinementKind = RefinementKind.OTHER
    affected_event_ids: list[str] = field(default_factory=list)
    horizon: ImpactHorizon | None = None
    source: str = "user_statement"
    confirmed_external: bool = False
    created_at: str = field(default_factory=utcnow_iso)

    def __post_init__(self) -> None:
        if not self.refinement_id:
            self.refinement_id = stable_id("refinement", self.analysis_id, self.text, self.created_at)

    def to_dict(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Refinement":
        return cls(
            refinement_id=str(data.get("refinement_id") or ""),
            company_id=str(data.get("company_id") or ""),
            analysis_id=str(data.get("analysis_id") or ""),
            text=str(data.get("text") or ""),
            kind=_enum(RefinementKind, data.get("kind"), RefinementKind.OTHER),
            affected_event_ids=_as_str_list(data.get("affected_event_ids")),
            horizon=_enum(ImpactHorizon, data.get("horizon"), None) if data.get("horizon") else None,
            source=str(data.get("source") or "user_statement"),
            confirmed_external=bool(data.get("confirmed_external", False)),
            created_at=str(data.get("created_at") or utcnow_iso()),
        )


# ---------------------------------------------------------------------------
# Queue jobs
# ---------------------------------------------------------------------------
JOB_NEWS_PROCESS = "news.process"
JOB_COMPANY_ASSESS = "company.assess"
JOB_COMPANY_ANALYZE = "company.analyze"

ALLOWED_JOB_TYPES = {JOB_NEWS_PROCESS, JOB_COMPANY_ASSESS, JOB_COMPANY_ANALYZE}


@dataclass
class NewsJob:
    job_id: str
    news_id: str
    job_type: str = JOB_NEWS_PROCESS
    content_version: str | None = None
    company_id: str | None = None
    attempts: int = 0
    enqueued_at: str = field(default_factory=utcnow_iso)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_message(self) -> dict:
        return jsonable(self)

    @classmethod
    def from_message(cls, data: dict) -> "NewsJob":
        from .errors import MalformedJobError

        if not isinstance(data, dict):
            raise MalformedJobError("Queue message is not a JSON object")
        news_id = data.get("news_id") or data.get("id")
        if not news_id or not isinstance(news_id, str):
            raise MalformedJobError("Queue message has no string 'news_id'")
        job_type = str(data.get("job_type") or data.get("type") or JOB_NEWS_PROCESS)
        if job_type not in ALLOWED_JOB_TYPES:
            raise MalformedJobError(f"Unknown job_type {job_type!r}")
        company_id = data.get("company_id")
        if job_type in {JOB_COMPANY_ASSESS, JOB_COMPANY_ANALYZE} and not company_id:
            raise MalformedJobError(f"Job type {job_type!r} requires 'company_id'")
        try:
            attempts = int(data.get("attempts") or 0)
        except (TypeError, ValueError):
            raise MalformedJobError("'attempts' must be an integer")
        return cls(
            job_id=str(data.get("job_id") or stable_id("job", news_id, job_type, company_id, data.get("enqueued_at") or "")),
            news_id=str(news_id),
            job_type=job_type,
            content_version=(str(data["content_version"]) if data.get("content_version") else None),
            company_id=(str(company_id) if company_id else None),
            attempts=attempts,
            enqueued_at=str(data.get("enqueued_at") or utcnow_iso()),
            payload=dict(data.get("payload") or {}),
        )


@dataclass
class ClassifyAndExtractResult:
    """Return type of ``classify_and_extract``: facts + optional assessment."""

    processing: NewsProcessingResult
    assessment: CompanyNewsAssessment | None = None

    def to_dict(self) -> dict:
        return jsonable(self)
