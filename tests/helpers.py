"""Shared test fixtures: fake LLM, deterministic payloads, sample companies."""

from __future__ import annotations

import json
from typing import Any

from ai_engine.config import Settings
from ai_engine.llm.client import LLMResponse
from ai_engine.models import CompanyProfile, NewsRecord, Relationship, RelationType, Significance
from ai_engine.service import NewsIntelligenceService
from ai_engine.storage.memory import (
    InMemoryAnalyticsIndex,
    InMemoryCalculationRepository,
    InMemoryCompanyRepository,
    InMemoryIntelStore,
    InMemoryNewsRepository,
    InMemoryRelationshipRepository,
)

FACT_PROMPT = "news-facts.v1"
COMPANY_PROMPT = "company-relevance.v1"
INSIGHT_PROMPT = "company-insight.v1"

GAS_NEWS = NewsRecord(
    news_id="news-gas-1",
    title="Цены на газ выросли",
    text=(
        "Цена на природный газ выросла на 20% в январе 2026 года. "
        "Поставки газа на европейский рынок сократились."
    ),
    source_id="source-1",
    content_version="v1",
)


def fact_payload(
    relevance: str = "KEEP",
    *,
    quote: str = "Цена на природный газ выросла на 20%",
    action: str = "выросла",
) -> dict[str, Any]:
    return {
        "event_type": "price_change",
        "main_events": ["Цена на природный газ выросла на 20%"],
        "general_relevance": relevance,
        "classification_reason": "The source reports a market price change.",
        "entities": {
            "companies": [],
            "organizations": [],
            "people": [],
            "products": ["природный газ"],
            "resources": ["газ"],
            "dates": ["январь 2026"],
            "geographies": ["Европа"],
            "event_types": ["price_change"],
        },
        "facts": [
            {
                "fact_type": "price_change",
                "subject": "цена на природный газ",
                "action": action,
                "object": "20%",
                "date": "январь 2026",
                "period": "январь 2026",
                "quantities": [{"value": 20.0, "unit": "%", "raw": "20%"}],
                "conditions": [],
                "negated": False,
                "origin": "source",
                "certainty": "asserted",
                "quote": quote,
                "related_fact_ids": [],
                "extra": {},
            }
        ],
        "uncertainties": [],
        "contradictions": [],
        "missing_information": [],
    }


def assessment_payload(
    company_id: str,
    *,
    news_id: str = "news-gas-1",
    relevance: str = "KEEP",
    direction: str = "negative",
    impact_type: str = "indirect",
    mechanism: str = "Higher gas prices raise input costs.",
    fact_ids: list[str] | None = None,
    evidence: list[dict] | None = None,
    confidence: float | None = 0.6,
) -> dict[str, Any]:
    ids = fact_ids or []
    if evidence is None:
        evidence = [
            {
                "fact_id": fid,
                "source_id": "source-1",
                "quote": "Цена на природный газ выросла на 20%",
                "supports": "input cost pressure",
            }
            for fid in ids
        ]
    return {
        "news_id": news_id,
        "company_id": company_id,
        "relevance_class": relevance,
        "impact_direction": direction,
        "impact_type": impact_type,
        "impact_mechanism": mechanism,
        "impact_horizon": "short_term",
        "confidence": confidence,
        "reasoning_summary": "Profile shows an explicit exposure to the reported change.",
        "supporting_fact_ids": ids,
        "evidence": evidence,
        "limitations": ["preliminary"],
        "missing_data": [],
    }


class FakeLLM:
    """Scripted LLM.  Never performs network calls."""

    def __init__(self, responses: dict[str, list[Any]] | None = None, *, model_id: str = "test-model") -> None:
        self._responses: dict[str, list[Any]] = {
            key: list(value) for key, value in (responses or {}).items()
        }
        self.model_id = model_id
        self.calls: list[tuple[str, str]] = []

    def push(self, prompt_version: str, payload: Any) -> None:
        self._responses.setdefault(prompt_version, []).append(payload)

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_version: str,
        response_model=None,
        temperature=None,
        max_tokens=None,
    ) -> LLMResponse:
        self.calls.append((prompt_version, user_prompt))
        queue = self._responses.get(prompt_version)
        if not queue:
            raise AssertionError(f"no scripted LLM response for {prompt_version}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return LLMResponse(
            content=item,
            model_id=self.model_id,
            prompt_version=prompt_version,
            raw_text=json.dumps(item, ensure_ascii=False),
            attempts=1,
        )

    def count(self, prompt_version: str) -> int:
        return sum(1 for version, _ in self.calls if version == prompt_version)


def make_settings(**overrides) -> Settings:
    defaults = dict(
        llm_model="deepseek/deepseek-v4.1-flash",
        llm_api_key="test-key",
        llm_max_retries=0,
        llm_timeout=5.0,
        classifier_mode="rules",
        processing_version="v1",
        worker_max_attempts=3,
        worker_retry_delay=0.0,
        max_additional_searches=2,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def gas_producer() -> CompanyProfile:
    return CompanyProfile(
        company_id="company-gas-producer",
        name="Газодобывающая компания",
        industry="Нефтегаз",
        activities=["добыча и продажа природного газа"],
        products_services=["природный газ", "газовый конденсат"],
        cost_drivers=["электроэнергия"],
        geography=["Россия"],
        updated_at="2026-01-01",
    )


def energy_intensive() -> CompanyProfile:
    return CompanyProfile(
        company_id="company-cement",
        name="Цементный завод",
        industry="Строительные материалы",
        products_services=["цемент", "клинкер"],
        processes=["обжиг клинкера"],
        cost_drivers=["природный газ", "электроэнергия"],
        geography=["Россия"],
        updated_at="2026-01-01",
    )


def it_company() -> CompanyProfile:
    return CompanyProfile(
        company_id="company-it",
        name="Ромашка Софт",
        industry="IT",
        products_services=["разработка программного обеспечения"],
        geography=["Россия"],
        updated_at="2026-01-01",
    )


def sparse_company() -> CompanyProfile:
    return CompanyProfile(company_id="company-sparse", name="Неизвестная компания")


def sector_only_company() -> CompanyProfile:
    return CompanyProfile(company_id="company-sector", name="Отраслевая компания", industry="Нефтегаз")


def make_service(
    *,
    responses: dict[str, list[Any]] | None = None,
    settings: Settings | None = None,
    companies: list[CompanyProfile] | None = None,
    news: list[NewsRecord] | None = None,
    relationships: list[Relationship] | None = None,
    analytics: InMemoryAnalyticsIndex | None = None,
    llm: FakeLLM | None = None,
    store: InMemoryIntelStore | None = None,
):
    settings = settings or make_settings()
    llm = llm or FakeLLM(responses or {FACT_PROMPT: [fact_payload()]})
    store = store or InMemoryIntelStore()
    news_repo = InMemoryNewsRepository(news or [GAS_NEWS])
    company_repo = InMemoryCompanyRepository(
        companies or [gas_producer(), energy_intensive(), it_company(), sparse_company()]
    )
    rel_repo = InMemoryRelationshipRepository(relationships or [])
    service = NewsIntelligenceService(
        settings=settings,
        store=store,
        news=news_repo,
        companies=company_repo,
        relationships=rel_repo,
        analytics=analytics,
        calculations=InMemoryCalculationRepository(),
        llm=llm,
    )
    return service, store, news_repo, company_repo, rel_repo, llm


def confirmed_supplier(company_id: str = "company-cement", name: str = "Газпром") -> Relationship:
    return Relationship(
        relationship_id="rel-supplier-1",
        company_id=company_id,
        counterparty_id="cp-gazprom",
        counterparty_name=name,
        relation_type=RelationType.SUPPLIER,
        confirmed=True,
        significance=Significance.HIGH,
        direction="inbound",
        share=0.4,
        evidence=["annual supply contract 2025"],
        source="contracts",
        updated_at="2026-01-01",
    )


def payment_link(company_id: str = "company-cement", name: str = "Газпром") -> Relationship:
    return Relationship(
        relationship_id="rel-payment-1",
        company_id=company_id,
        counterparty_id="cp-gazprom",
        counterparty_name=name,
        relation_type=RelationType.PAYMENT_LINK,
        confirmed=True,
        significance=Significance.UNKNOWN,
        evidence=["single payment link"],
        source="payments",
    )
