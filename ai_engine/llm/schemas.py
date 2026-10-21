"""Pydantic schemas for structured model responses.

These models validate response shape and types. Source grounding and business
rules remain in the fact, assessment, and insight validators.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class FactsResponse(_ResponseModel):
    general_relevance: str
    facts: list[dict[str, Any]]
    event_type: str = ""
    main_events: Any = Field(default_factory=list)
    classification_reason: str = ""
    entities: dict[str, Any] | None = None
    uncertainties: Any = Field(default_factory=list)
    contradictions: Any = Field(default_factory=list)
    missing_information: Any = Field(default_factory=list)


class AssessmentResponse(_ResponseModel):
    relevance_class: str
    reasoning_summary: str
    news_id: str | None = None
    company_id: str | None = None
    impact_direction: str | None = None
    impact_type: str | None = None
    impact_horizon: str | None = None
    confidence: Any = None
    impact_mechanism: Any = ""
    supporting_fact_ids: Any = Field(default_factory=list)
    evidence: Any = Field(default_factory=list)
    limitations: Any = Field(default_factory=list)
    missing_data: Any = Field(default_factory=list)


class InsightResponse(_ResponseModel):
    events: list[dict[str, Any]]
    summary: Any = ""
    key_risks: Any = Field(default_factory=list)
    potential_opportunities: Any = Field(default_factory=list)
    unknowns: Any = Field(default_factory=list)
    recommended_checks: Any = Field(default_factory=list)
    limitations: Any = Field(default_factory=list)
    missing_data: Any = Field(default_factory=list)
