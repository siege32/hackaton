from __future__ import annotations

from typing import Protocol

from .schemas import NewsParseRequest, ParsedNewsResponse


class NewsParser(Protocol):
    """Adapter contract for the separately owned news parsing service."""

    def parse(self, request: NewsParseRequest) -> ParsedNewsResponse: ...