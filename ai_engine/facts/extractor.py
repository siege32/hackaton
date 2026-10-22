"""Stage A: extract company-independent facts from one news item."""

from __future__ import annotations

import logging

from ..config import Settings
from ..errors import NewsTextEmptyError
from ..llm.client import LLMClient
from ..llm.prompts import PROMPT_VERSIONS, facts_prompts
from ..llm.schemas import FactsResponse
from ..models import NewsProcessingResult, NewsRecord, ProcessingStatus
from ..utils import utcnow_iso
from .validation import validate_facts_payload

logger = logging.getLogger("sxsvn_intel.facts")


class FactExtractor:
    def __init__(
        self,
        llm: LLMClient | None,
        settings: Settings,
        *,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self._logger = logger_ or logger

    def extract(self, news: NewsRecord, *, job_id: str | None = None) -> NewsProcessingResult:
        if not (news.text or "").strip():
            raise NewsTextEmptyError(f"news {news.news_id} has no text")
        if self.llm is None:
            raise RuntimeError(
                "FactExtractor requires an LLM client; rule-based mode cannot invent facts. "
                "Configure LLM_API_KEY or provide an LLMClient."
            )

        prompt_version = PROMPT_VERSIONS["facts"]
        truncated = len(news.full_text) > self.settings.llm_max_input_chars
        system_prompt, user_prompt = facts_prompts(
            {
                "news_id": news.news_id,
                "title": news.title,
                "source_id": news.source_id,
                "published_at": news.published_at,
                "url": news.url,
                "text": news.full_text,
            },
            self.settings.llm_max_input_chars,
        )
        self._logger.info(
            "extract facts news_id=%s version=%s job=%s prompt=%s",
            news.news_id,
            news.content_version,
            job_id,
            prompt_version,
        )
        response = self.llm.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_version=prompt_version,
            response_model=FactsResponse,
        )
        outcome = validate_facts_payload(response.content, news, text_truncated=truncated)

        result = NewsProcessingResult(
            news_id=news.news_id,
            content_version=news.content_version,
            status=ProcessingStatus.PROCESSED,
            general_relevance=outcome.general_relevance,
            classification_reason=outcome.classification_reason,
            event_type=outcome.event_type,
            main_events=outcome.main_events,
            entities=outcome.entities,
            facts=outcome.facts,
            uncertainties=outcome.uncertainties,
            contradictions=outcome.contradictions,
            missing_data=outcome.missing_data,
            validation_warnings=outcome.warnings,
            source_id=news.source_id,
            model_id=response.model_id,
            prompt_version=prompt_version,
            processing_version=self.settings.processing_version,
            job_id=job_id,
            created_at=utcnow_iso(),
            updated_at=utcnow_iso(),
        )
        self._logger.info(
            "extracted facts news_id=%s facts=%s relevance=%s rejected=%s",
            news.news_id,
            len(result.facts),
            result.general_relevance.value,
            outcome.rejected_facts,
        )
        return result
