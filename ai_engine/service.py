"""Public service facade.

These methods are the stable integration surface for the rest of the project
(server layer, scripts, future UI).  No HTTP API or UI is implemented here.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from .classification.classifier import RULE_PROMPT_VERSION, CompanyNewsClassifier
from .config import Settings
from .errors import (
    CompanyNotFoundError,
    ConfigError,
    NewsNotFoundError,
    ProcessingNotReadyError,
    StorageError,
    ToolUnavailableError,
)
from .facts.extractor import FactExtractor
from .insight.engine import InsightEngine
from .llm.client import LLMClient
from .llm.prompts import PROMPT_VERSIONS
from .models import (
    AnalysisStatus,
    ClassifyAndExtractResult,
    CompanyNewsAssessment,
    CompanyProfile,
    ImpactHorizon,
    InsightResult,
    NewsProcessingResult,
    NewsRecord,
    ProcessingStatus,
    Refinement,
    RefinementKind,
    RelevanceClass,
    Relationship,
)
from .storage.base import (
    AnalyticsIndex,
    CalculationRepository,
    CompanyRepository,
    ExternalSearchTool,
    IntelStore,
    NewsRepository,
    RelationshipRepository,
)
from .storage.memory import InMemoryRelationshipRepository
from .storage.sql import (
    SqlCompanyRepository,
    SqlNewsRepository,
    SqlStore,
    UnavailableAnalyticsIndex,
)
from .utils import utcnow_iso

logger = logging.getLogger("sxsvn_intel.service")


class _EmptyRelationshipRepository:
    """Explicit empty adapter: no relationship data is available.

    It returns an empty list rather than fabricated counterparties.
    """

    def list_relationships(self, company_id: str, limit: int = 50) -> list[Relationship]:
        return []


class NewsIntelligenceService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: IntelStore,
        news: NewsRepository,
        companies: CompanyRepository,
        relationships: RelationshipRepository | None = None,
        analytics: AnalyticsIndex | None = None,
        calculations: CalculationRepository | None = None,
        external_search: ExternalSearchTool | None = None,
        llm: LLMClient | None = None,
        classifier: CompanyNewsClassifier | None = None,
        engine: InsightEngine | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.news = news
        self.companies = companies
        self.relationships = relationships or _EmptyRelationshipRepository()
        self.analytics = analytics
        self.calculations = calculations
        self.external_search = external_search
        self.llm = llm if llm is not None else self._default_llm(settings)
        self.extractor = FactExtractor(self.llm, settings)
        self.classifier = classifier or CompanyNewsClassifier(settings, self.llm)
        self.engine = engine or InsightEngine(
            settings,
            store,
            companies,
            self.relationships,
            news=news,
            analytics=analytics,
            calculations=calculations,
            external_search=external_search,
            llm=self.llm,
            classifier=self.classifier,
        )

    @staticmethod
    def _default_llm(settings: Settings) -> LLMClient | None:
        if settings.classifier_mode == "llm" and not settings.llm_api_key:
            raise ConfigError("CLASSIFIER_MODE=llm requires LLM_API_KEY")
        if settings.use_llm:
            return LLMClient(settings)
        return None

    # ------------------------------------------------------------------
    # Processing status vs business relevance:
    # * NewsProcessingResult.status is technical (`processed`/`failed`/...)
    # * general_relevance and CompanyNewsAssessment.relevance_class are
    #   business classes (`KEEP`/`DROP`/`REVIEW`).
    # ------------------------------------------------------------------
    def process_news(
        self,
        news_id: str,
        *,
        content_version: str | None = None,
        force: bool = False,
        job_id: str | None = None,
    ) -> NewsProcessingResult:
        record = self.news.get_news(news_id)
        if record is None:
            raise NewsNotFoundError(f"news {news_id!r} not found")
        if content_version and content_version != record.content_version:
            logger.warning(
                "job content_version differs from stored news news_id=%s job_version=%s stored_version=%s; "
                "processing the stored version",
                news_id,
                content_version,
                record.content_version,
            )
        return self._process_record(record, force=force, job_id=job_id)

    def _process_record(
        self, record: NewsRecord, *, force: bool = False, job_id: str | None = None
    ) -> NewsProcessingResult:
        existing = self.store.get_processing_result(record.news_id)
        if existing is not None and not force and self._processing_reusable(existing, record):
            logger.info(
                "skip already processed news news_id=%s version=%s processing_version=%s",
                record.news_id,
                record.content_version,
                existing.processing_version,
            )
            return existing

        if not (record.text or "").strip():
            result = self._failed_result(
                record,
                error_code="news_text_empty",
                error_message="News text is empty; nothing to extract.",
                job_id=job_id,
            )
            self.store.save_processing_result(result)
            logger.warning("empty news text news_id=%s; stored failed result", record.news_id)
            return result

        if self.llm is None:
            raise ConfigError(
                "News fact extraction requires an LLM. Configure LLM_API_KEY or pass an LLMClient."
            )

        self.store.begin_processing(
            record.news_id,
            record.content_version,
            self.settings.processing_version,
            job_id=job_id,
        )
        result = self.extractor.extract(record, job_id=job_id)
        self.store.save_processing_result(result)
        return result

    def _processing_reusable(
        self, existing: NewsProcessingResult, record: NewsRecord
    ) -> bool:
        if existing.status != ProcessingStatus.PROCESSED:
            return False
        if existing.content_version != record.content_version:
            return False
        if existing.processing_version != self.settings.processing_version:
            return False
        if existing.prompt_version != PROMPT_VERSIONS["facts"]:
            return False
        expected_model = self.llm.model_id if self.llm is not None else ""
        return bool(expected_model) and existing.model_id == expected_model

    def _failed_result(
        self,
        record: NewsRecord,
        *,
        error_code: str,
        error_message: str,
        job_id: str | None = None,
    ) -> NewsProcessingResult:
        return NewsProcessingResult(
            news_id=record.news_id,
            content_version=record.content_version,
            status=ProcessingStatus.FAILED,
            general_relevance=RelevanceClass.REVIEW,
            classification_reason="processing failed; classification was not produced",
            source_id=record.source_id,
            model_id=self.llm.model_id if self.llm else "",
            prompt_version=PROMPT_VERSIONS["facts"],
            processing_version=self.settings.processing_version,
            job_id=job_id,
            error_code=error_code,
            error_message=error_message[:1000],
            created_at=utcnow_iso(),
            updated_at=utcnow_iso(),
        )

    def record_failed_processing(
        self,
        news_id: str,
        *,
        content_version: str,
        error_code: str,
        error_message: str,
        job_id: str | None = None,
    ) -> NewsProcessingResult | None:
        """Persist a terminal failure without erasing a successful result."""
        existing = self.store.get_processing_result(news_id)
        if (
            existing is not None
            and existing.status == ProcessingStatus.PROCESSED
            and existing.content_version == content_version
        ):
            return existing
        record = self.news.get_news(news_id)
        if record is None:
            return None
        result = self._failed_result(
            record,
            error_code=error_code,
            error_message=error_message,
            job_id=job_id,
        )
        result.content_version = content_version or record.content_version
        self.store.save_processing_result(result)
        return result

    # ------------------------------------------------------------------
    def classify_and_extract(
        self,
        news: NewsRecord | str,
        *,
        company_id: str | None = None,
        force: bool = False,
    ) -> ClassifyAndExtractResult:
        if isinstance(news, NewsRecord):
            record = news
            processing = self._process_record(record, force=force)
        else:
            record = self.news.get_news(str(news))
            if record is None:
                raise NewsNotFoundError(f"news {news!r} not found")
            processing = self._process_record(record, force=force)
        assessment = None
        if company_id:
            assessment = self.classify_news_for_company(
                processing.news_id,
                company_id,
                force=force,
                processing=processing,
                news_record=record,
            )
        return ClassifyAndExtractResult(processing=processing, assessment=assessment)

    def classify_news_for_company(
        self,
        news_id: str,
        company_id: str,
        *,
        force: bool = False,
        processing: NewsProcessingResult | None = None,
        news_record: NewsRecord | None = None,
        job_id: str | None = None,
    ) -> CompanyNewsAssessment:
        company = self.companies.get_company(company_id)
        if company is None:
            raise CompanyNotFoundError(f"company {company_id!r} not found")

        record = news_record
        if record is None:
            record = self.news.get_news(news_id)
            if record is None:
                raise NewsNotFoundError(f"news {news_id!r} not found")

        if processing is None:
            processing = self.store.get_processing_result(news_id)
        if processing is None:
            processing = self.process_news(news_id, job_id=job_id)
        if processing.status != ProcessingStatus.PROCESSED:
            raise ProcessingNotReadyError(
                f"news {news_id!r} is not successfully processed "
                f"(status={processing.status.value}, error={processing.error_code})"
            )
        if processing.content_version != record.content_version:
            # Re-extract when the news text changed; never assess old facts.
            logger.warning(
                "news content changed news_id=%s processed=%s current=%s",
                news_id,
                processing.content_version,
                record.content_version,
            )
            processing = self.process_news(news_id, force=True, job_id=job_id)

        existing = self.store.get_assessment(news_id, company_id)
        if existing is not None and not force and self._assessment_reusable(existing, processing, company):
            logger.info(
                "skip existing assessment news_id=%s company_id=%s profile_version=%s",
                news_id,
                company_id,
                company.profile_version,
            )
            return existing

        try:
            relationships = self.relationships.list_relationships(
                company_id, limit=self.settings.max_relationships
            )
        except ToolUnavailableError as exc:
            logger.warning(
                "relationship tool unavailable company_id=%s error=%s", company_id, exc.message
            )
            relationships = []
        except Exception as exc:
            logger.warning(
                "relationship lookup failed company_id=%s error=%s",
                company_id,
                type(exc).__name__,
            )
            relationships = []

        assessment = self.classifier.classify(
            company,
            processing,
            source_text=record.full_text,
            relationships=relationships,
        )
        self.store.save_assessment(assessment)
        logger.info(
            "assessment saved news_id=%s company_id=%s relevance=%s source=%s",
            news_id,
            company_id,
            assessment.relevance_class.value,
            assessment.classification_source,
        )
        return assessment

    def _assessment_reusable(
        self,
        existing: CompanyNewsAssessment,
        processing: NewsProcessingResult,
        company: CompanyProfile,
    ) -> bool:
        if existing.content_version != processing.content_version:
            return False
        if existing.profile_version != company.profile_version:
            return False
        if existing.processing_version != self.settings.processing_version:
            return False
        if self.settings.use_llm and self.llm is not None:
            return (
                existing.classification_source in {"llm", "llm_rejected"}
                and existing.model_id == self.llm.model_id
                and existing.prompt_version == PROMPT_VERSIONS["company_assessment"]
            )
        return (
            existing.classification_source == "rules"
            and existing.prompt_version == RULE_PROMPT_VERSION
        )

    # ------------------------------------------------------------------
    def get_processing_result(self, news_id: str) -> NewsProcessingResult | None:
        return self.store.get_processing_result(news_id)

    def get_company_news_assessment(
        self, news_id: str, company_id: str
    ) -> CompanyNewsAssessment | None:
        return self.store.get_assessment(news_id, company_id)

    # ------------------------------------------------------------------
    def analyze_company_impact(
        self,
        company_id: str,
        event_ids: Sequence[str] | str,
        **kwargs: Any,
    ) -> InsightResult:
        if isinstance(event_ids, str):
            event_ids = [event_ids]
        kwargs.setdefault("auto_classify", True)
        return self.engine.analyze(company_id, event_ids, **kwargs)

    def get_analysis(self, analysis_id: str) -> InsightResult | None:
        return self.store.get_analysis(analysis_id)

    def adapt_company_analysis(
        self,
        analysis_id: str,
        *,
        refinement: Refinement | None = None,
        text: str | None = None,
        kind: RefinementKind | str = RefinementKind.OTHER,
        affected_event_ids: Sequence[str] | None = None,
        horizon: ImpactHorizon | str | None = None,
    ) -> InsightResult:
        if refinement is not None:
            if refinement.analysis_id and refinement.analysis_id != analysis_id:
                raise ValueError("refinement.analysis_id does not match analysis_id")
            return self.engine.adapt(
                analysis_id,
                text=refinement.text,
                kind=refinement.kind,
                affected_event_ids=refinement.affected_event_ids or affected_event_ids,
                horizon=refinement.horizon or horizon,
            )
        if not text:
            raise ValueError("either refinement or text must be provided")
        return self.engine.adapt(
            analysis_id,
            text=text,
            kind=kind,
            affected_event_ids=affected_event_ids,
            horizon=horizon,
        )


def build_service(
    settings: Settings,
    *,
    store: IntelStore | None = None,
    news: NewsRepository | None = None,
    companies: CompanyRepository | None = None,
    relationships: RelationshipRepository | None = None,
    analytics: AnalyticsIndex | None = None,
    calculations: CalculationRepository | None = None,
    external_search: ExternalSearchTool | None = None,
    llm: LLMClient | None = None,
) -> NewsIntelligenceService:
    """Build a service over an SQLite database and the existing news table.

    For PostgreSQL or custom schemas, construct ``SqlStore``/custom adapters and
    pass them explicitly.
    """
    if store is None:
        if not settings.sqlite_path:
            raise ConfigError(
                "INTEL_SQLITE_PATH is not set; provide explicit storage adapters "
                "or set INTEL_SQLITE_PATH"
            )
        sql_store = SqlStore.sqlite(settings.sqlite_path)
        store = sql_store
        if news is None:
            news = SqlNewsRepository(sql_store.connection, table=settings.news_table)
        if companies is None:
            companies = SqlCompanyRepository(
                sql_store.connection, table=settings.company_table
            )
    if news is None or companies is None:
        raise ConfigError("news and company repositories must be provided")
    return NewsIntelligenceService(
        settings=settings,
        store=store,
        news=news,
        companies=companies,
        relationships=relationships or InMemoryRelationshipRepository(),
        analytics=analytics if analytics is not None else UnavailableAnalyticsIndex(),
        calculations=calculations,
        external_search=external_search,
        llm=llm,
    )
