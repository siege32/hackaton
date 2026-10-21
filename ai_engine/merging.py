"""Small merge helpers shared by extraction, search and result assembly."""

from __future__ import annotations

from typing import Any

from .models import EventExtraction, EventFact


def merge_missing(*groups: Any) -> tuple[str, ...]:
    """Flatten and deduplicate missing-data notes while keeping their order."""

    flat: list[Any] = []
    for group in groups:
        flat.extend((group,) if isinstance(group, str) else group)
    return tuple(dict.fromkeys(str(item) for item in flat))


def union(*groups: Any) -> tuple[str, ...]:
    flat: list[str] = []
    for group in groups:
        flat.extend(group)
    return tuple(dict.fromkeys(item for item in flat if item))


def merge_facts(*groups: tuple[EventFact, ...]) -> tuple[EventFact, ...]:
    merged: list[EventFact] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for group in groups:
        for fact in group:
            key = (fact.text.casefold().strip(), fact.source_ids)
            if key in seen:
                continue
            seen.add(key)
            merged.append(fact)
    return tuple(merged)


def merge_extractions(base: EventExtraction, extra: EventExtraction) -> EventExtraction:
    return base.model_copy(
        update={
            "facts": merge_facts(base.facts, extra.facts),
            "participants": union(base.participants, extra.participants),
            "industries": union(base.industries, extra.industries),
            "geographies": union(base.geographies, extra.geographies),
            "unconfirmed": union(base.unconfirmed, extra.unconfirmed),
            "source_ids": union(base.source_ids, extra.source_ids),
        }
    )
