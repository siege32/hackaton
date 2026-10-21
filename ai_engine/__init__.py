"""SXSVN AI news-intelligence module.

Public integration surface:

* :class:`NewsIntelligenceService` - programmatic API;
* :func:`run_news_worker` - consumer of the existing Redis queue;
* storage/queue protocols - adapters for the rest of the project.
"""

from .config import KNOWN_MODELS, Settings
from .errors import IntelError
from .models import (
    AnalysisStatus,
    ClassifyAndExtractResult,
    CompanyNewsAssessment,
    CompanyProfile,
    ImpactDirection,
    ImpactEvent,
    ImpactHorizon,
    ImpactType,
    InsightResult,
    NewsFact,
    NewsProcessingResult,
    NewsRecord,
    ProcessingStatus,
    Refinement,
    RefinementKind,
    RelevanceClass,
    Relationship,
)
from .service import NewsIntelligenceService, build_service
from .worker import NewsWorker, run_news_worker

__version__ = "0.1.0"

__all__ = [
    "AnalysisStatus",
    "ClassifyAndExtractResult",
    "CompanyNewsAssessment",
    "CompanyProfile",
    "ImpactDirection",
    "ImpactEvent",
    "ImpactHorizon",
    "ImpactType",
    "InsightResult",
    "IntelError",
    "KNOWN_MODELS",
    "NewsFact",
    "NewsIntelligenceService",
    "NewsProcessingResult",
    "NewsRecord",
    "NewsWorker",
    "ProcessingStatus",
    "Refinement",
    "RefinementKind",
    "RelevanceClass",
    "Relationship",
    "Settings",
    "build_service",
    "run_news_worker",
]
