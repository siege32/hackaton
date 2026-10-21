"""Explicit state of one bounded agent run."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    AnalysisContext,
    EventExtraction,
    RelevanceAssessment,
    ToolResult,
    TraceStep,
)


@dataclass
class AnalysisState:
    """Mutable state for a single analysis; never shared between runs."""

    context: AnalysisContext
    trace_id: str
    extraction: EventExtraction | None = None
    relevance: RelevanceAssessment | None = None
    tools: dict[str, ToolResult] = field(default_factory=dict)
    journal: list[TraceStep] = field(default_factory=list)
    search_steps_used: int = 0
    limit_hit: bool = False

    def step(
        self,
        action: str,
        status: str = "ok",
        *,
        tool: str | None = None,
        detail: str = "",
    ) -> None:
        self.journal.append(
            TraceStep(
                step=len(self.journal) + 1,
                action=action,
                status=status,
                tool=tool,
                detail=detail,
            )
        )

    def record_tool(self, result: ToolResult) -> None:
        self.tools[result.tool] = result
        self.step("tool", result.status, tool=result.tool, detail=result.message[:200])
