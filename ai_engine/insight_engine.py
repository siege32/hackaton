"""Callable entry point for the SXSVN insight engine."""

from __future__ import annotations

from .agent import InsightEngine
from .agent_tools import AgentToolkit
from .llm import LLMClient
from .models import AnalysisContext, AnalysisResult


def insight_engine(
    context: AnalysisContext,
    *,
    llm: LLMClient | None = None,
    toolkit: AgentToolkit | None = None,
    max_search_steps: int | None = None,
    llm_retries: int | None = None,
) -> AnalysisResult:
    """Run one bounded analysis and return the validated result.

    Without ``llm`` the engine uses deterministic rules. External data tools
    must be supplied explicitly; no synthetic data is used by default.
    With ``llm`` the same contracts are used, but fact extraction and the final
    analysis are produced by the approved model.
    """

    engine = InsightEngine(
        llm=llm,
        toolkit=toolkit,
        max_search_steps=max_search_steps,
        llm_retries=llm_retries,
    )
    return engine.analyze(context)


__all__ = ["insight_engine", "InsightEngine"]
