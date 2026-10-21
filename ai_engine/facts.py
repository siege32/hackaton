"""Fact extraction from a news item.

Two implementations are provided:

* ``HeuristicFactExtractor`` is an offline, clearly labelled fallback. It quotes
  sentences from the source and never invents event dates.
* ``LLMFactExtractor`` asks the configured model for structured facts.

``FactExtractionService`` caches by news content, so the same material is not
extracted again for a second company.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Protocol

from cachetools import TTLCache
from pydantic import ValidationError

from .config import get_settings
from .llm import LLMClient, LLMError
from .models import EventExtraction, EventFact, NewsItem
from .prompts import fact_extraction_messages

logger = logging.getLogger(__name__)

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


class FactExtractor(Protocol):
    name: str

    def extract(self, news: NewsItem, event_id: str) -> EventExtraction: ...


class HeuristicFactExtractor:
    """Offline extractor: quotes sentences, marks facts as not independently verified."""

    name = "heuristic"

    def __init__(self, max_facts: int = 5, min_chars: int = 20):
        self.max_facts = max_facts
        self.min_chars = min_chars

    def extract(self, news: NewsItem, event_id: str) -> EventExtraction:
        sentences = [part.strip() for part in _SENTENCE_RE.split(news.text) if part.strip()]
        selected = [sentence for sentence in sentences if len(sentence) >= self.min_chars][: self.max_facts]
        if not selected and news.text.strip():
            selected = [news.text.strip()]
        facts = tuple(
            EventFact(
                fact_id=f"{news.news_id}-f{index}",
                text=sentence,
                source_ids=(news.source_id,),
                quote=sentence,
                verified=False,
                topic=news.title,
                occurred_at=None,
                published_at=news.published_at,
                unconfirmed=("event date is unknown; publication date is not used instead",),
            )
            for index, sentence in enumerate(selected, 1)
        )
        unconfirmed = ["event date is unknown"] if news.published_at else ["event and publication dates are unknown"]
        unconfirmed.append("participants, industries and geographies were not extracted without a model")
        return EventExtraction(
            event_id=event_id,
            topic=news.title,
            occurred_at=None,
            published_at=news.published_at,
            facts=facts,
            unconfirmed=tuple(unconfirmed),
            source_ids=(news.source_id,),
            extractor="heuristic",
        )


class LLMFactExtractor:
    """Structured extraction through the approved model adapter."""

    name = "llm"

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def extract(self, news: NewsItem, event_id: str) -> EventExtraction:
        payload = self.llm.complete(fact_extraction_messages(news))
        payload.setdefault("event_id", event_id)
        try:
            extraction = EventExtraction.model_validate(payload)
        except (ValidationError, TypeError, ValueError) as exc:
            raise LLMError(f"invalid fact extraction payload: {exc}") from exc
        facts = tuple(
            fact
            if news.source_id in fact.source_ids
            else fact.model_copy(update={"source_ids": (news.source_id, *fact.source_ids)})
            for fact in extraction.facts
        )
        return extraction.model_copy(update={"facts": facts, "extractor": "llm"})


class FactExtractionService:
    """Content-addressed cache around a fact extractor."""

    def __init__(self, extractor: FactExtractor | None = None):
        self.extractor = extractor or HeuristicFactExtractor()
        settings = get_settings()
        self._cache: TTLCache[str, EventExtraction] = TTLCache(
            maxsize=settings.cache_max_size,
            ttl=settings.cache_ttl_seconds,
        )

    def extract(self, news: NewsItem, event_id: str | None = None) -> EventExtraction:
        key = hashlib.sha256(
            json.dumps({"news": news.model_dump(mode="json"), "extractor": self.extractor.name}, sort_keys=True).encode()
        ).hexdigest()
        if key in self._cache:
            logger.info("fact_extraction_cache_hit", extra={"source_id": news.source_id})
            return self._cache[key]
        extraction = self.extractor.extract(news, event_id or news.news_id)
        self._cache[key] = extraction
        return extraction
