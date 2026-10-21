"""Prompt templates for the SXSVN insight engine.

Prompts live apart from transport and orchestration so they can be reviewed and
changed without touching the agent logic. Nothing in this module performs I/O.
"""

from __future__ import annotations

import json
from typing import Any

from .models import AnalysisContext, EventExtraction, NewsItem, RelevanceAssessment

FACT_EXTRACTION_SYSTEM = (
    "You extract structured facts from one news item. Return JSON only. "
    "Every fact must cite the provided source_id. Never invent dates: if the event date is "
    "not stated in the text, set occurred_at to null and keep published_at separately. "
    "Put uncertain details into unconfirmed instead of facts. Participants, industries and "
    "geographies must be copied from the text, not guessed from general knowledge."
)

RELEVANCE_SYSTEM = (
    "You assess whether one event can affect one company. Return JSON only with keys: "
    "decision (KEEP, DROP or REVIEW), score (0..1), rationale, bases (array of strings), "
    "missing_data (array of strings), channel (direct, counterparty, industry, geography or none). "
    "Use KEEP for a direct or counterparty link, REVIEW when the data is insufficient, and DROP "
    "only when there is no company, counterparty, industry or geography connection. "
    "An industry mismatch alone is not a reason to DROP: impact can travel through counterparties. "
    "Never claim that a payment link proves supply dependency."
)

ANALYSIS_SYSTEM = (
    "You are the SXSVN business-analysis agent. Return one JSON object only. "
    "Every material risk, opportunity or implication must cite evidence_id values from the "
    "provided evidence catalog. Separate facts, bank material, calculations and hypotheses via "
    "claim_type. Never present a hypothesis as a fact. Never present general model knowledge as "
    "an Alfa-Bank position: if no bank material was found, say so in missing_data. "
    "priority_score is attention priority, never loss probability; keep evidence quality separate. "
    "For implications and hypotheses fill limitation; for implications also fill mechanism and "
    "consequence. If the company-event link is not established, return no impact claims."
)

ANALYSIS_SCHEMA_HINT = {
    "event": {"event_id": "string", "facts": ["fact objects"]},
    "priority": {"priority_score": "0..1", "meaning": "attention priority, not loss probability"},
    "company_features": {},
    "bank_view": {},
    "ai_implications": ["claim"],
    "risks": ["claim"],
    "opportunities": ["claim"],
    "missing_data": ["string"],
    "next_check": "string or null",
    "evidence": [
        {
            "evidence_id": "string",
            "source_id": "string",
            "quote": "string",
            "kind": "event|bank|calculation|hypothesis",
        }
    ],
    "status": "completed|needs_review|failed",
}

CLAIM_SCHEMA_HINT = {
    "claim_id": "string",
    "text": "string",
    "evidence_ids": ["evidence id"],
    "limitation": "string",
    "claim_type": "fact|hypothesis|implication",
    "feature": "company feature used",
    "mechanism": "how the event can affect the company",
    "consequence": "possible consequence, explicitly uncertain",
}


def fact_extraction_messages(news: NewsItem) -> list[dict[str, str]]:
    payload = {
        "news": news.model_dump(mode="json"),
        "output": {
            "event_id": news.news_id,
            "topic": "short topic",
            "occurred_at": "ISO date or null",
            "published_at": "ISO date or null",
            "participants": [],
            "industries": [],
            "geographies": [],
            "facts": [
                {
                    "fact_id": "string",
                    "text": "fact as stated in the source",
                    "source_ids": [news.source_id],
                    "verified": True,
                    "topic": "string",
                    "occurred_at": "ISO date or null",
                    "published_at": "ISO date or null",
                    "participants": [],
                    "industries": [],
                    "geographies": [],
                    "unconfirmed": [],
                    "quote": "verbatim fragment",
                }
            ],
            "unconfirmed": [],
            "source_ids": [news.source_id],
        },
    }
    return [
        {"role": "system", "content": FACT_EXTRACTION_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def relevance_messages(profile: dict[str, Any], extraction: EventExtraction) -> list[dict[str, str]]:
    payload = {
        "company": profile,
        "event": extraction.model_dump(mode="json"),
        "rules": {
            "counterparty_link_is_not_proof": True,
            "industry_mismatch_is_not_drop": True,
        },
    }
    return [
        {"role": "system", "content": RELEVANCE_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)[:40_000]},
    ]


def analysis_messages(
    context: AnalysisContext,
    extraction: EventExtraction,
    relevance: RelevanceAssessment,
    evidence_catalog: list[dict[str, Any]],
    tool_trace: list[dict[str, Any]],
    *,
    max_chars: int = 40_000,
) -> list[dict[str, str]]:
    payload = {
        "context": context.to_dict(),
        "extraction": extraction.model_dump(mode="json"),
        "relevance": relevance.model_dump(mode="json"),
        "evidence_catalog": evidence_catalog,
        "tool_trace": tool_trace,
        "output_schema": ANALYSIS_SCHEMA_HINT,
        "claim_schema": CLAIM_SCHEMA_HINT,
    }
    if context.clarification:
        payload["user_clarification"] = context.clarification
        payload["clarification_rule"] = (
            "Update only claims, calculations and conclusions that depend on the clarification; "
            "keep unrelated fields unchanged and re-state limitations."
        )
    return [
        {"role": "system", "content": ANALYSIS_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)[:max_chars]},
    ]
