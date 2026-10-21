from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ai_engine.config import get_settings
from ai_engine.insight_engine import insight_engine
from ai_engine.llm import OpenAICompatibleClient
from ai_engine.models import AnalysisContext, AnalysisResult
from api.parser import NewsParser
from api.schemas import NewsAnalysisRequest, NewsParseRequest, ParsedNewsResponse

app = FastAPI(title="SXSVN Insight API", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/analyze", response_model=AnalysisResult)
@app.post("/v1/analyze", response_model=AnalysisResult, include_in_schema=False)
def analyze(context: AnalysisContext) -> AnalysisResult:
    return _run_analysis(context)


@app.post("/v1/analyze/news", response_model=AnalysisResult)
def analyze_news(request: NewsAnalysisRequest) -> AnalysisResult:
    return _run_analysis(request.to_context())


@app.post("/v1/news/parse", response_model=ParsedNewsResponse)
def parse_news(request: NewsParseRequest) -> ParsedNewsResponse:
    parser: NewsParser | None = getattr(app.state, "news_parser", None)
    if parser is None:
        raise HTTPException(status_code=503, detail="news parser service is not configured")
    return parser.parse(request)


def _run_analysis(context: AnalysisContext) -> AnalysisResult:
    settings = get_settings()
    llm = OpenAICompatibleClient(settings) if settings.llm_api_key else None
    return insight_engine(context, llm=llm)