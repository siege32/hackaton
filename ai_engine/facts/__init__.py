"""Fact extraction (stage A)."""

from .extractor import FactExtractor
from .validation import FactValidationOutcome, validate_facts_payload

__all__ = ["FactExtractor", "FactValidationOutcome", "validate_facts_payload"]
