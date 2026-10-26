from __future__ import annotations

import sqlite3
import logging
import os
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ai_engine import Settings as IntelSettings
from ai_engine.errors import CompanyNotFoundError, ConfigError, IntelError, NewsNotFoundError
from ai_engine.service import NewsIntelligenceService, build_service
from ai_engine.storage.sql import SqlStore
from data_pipeline import DataSettings, PaymentCalculationRepository, PaymentRelationshipRepository
from data_pipeline.config import load_dotenv
from data_pipeline.exposure import calculate_sector_exposure, get_counterparty_exposure
from data_pipeline.news import (
    SqliteNewsRepository,
    add_manual_text,
    cluster_sources,
    fetch_all,
    list_news,
    open_news_db,
    search_news,
)
from data_pipeline.news.sources import enabled_sources, source_by_id
from data_pipeline.profiles import SqliteCompanyRepository
from data_pipeline.reports import BankReportsIndex, DOCUMENTS
from data_pipeline.store import connect, quality_report

from api.schemas import AnalysisRequest, NewsCreateRequest, NewsFetchRequest, RefinementRequest
from api.jobs import JobStore

BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).with_name("static")
logger = logging.getLogger("sxsvn.api")
load_dotenv(BASE_DIR / ".env")

app = FastAPI(title="SXSVN Intelligence", version="1.0.0")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


def _absolute(path: Path) -> Path:
    return path if path.is_absolute() else BASE_DIR / path


def _data_settings() -> DataSettings:
    settings = DataSettings.from_env()
    return DataSettings(
        raw_dir=_absolute(settings.raw_dir),
        db_path=_absolute(settings.db_path),
        reports_dir=_absolute(settings.reports_dir),
        reports_ocr_dir=_absolute(settings.reports_ocr_dir),
        news_db_path=_absolute(settings.news_db_path),
    )


def _intel_path() -> Path:
    intel_settings = IntelSettings.from_env()
    return _absolute(Path(os.environ.get("INTEL_SQLITE_PATH") or intel_settings.sqlite_path or "data/processed/intel.sqlite"))


def _job_store() -> JobStore:
    path = Path(os.environ.get("API_JOBS_DB_PATH") or _intel_path())
    return JobStore(_absolute(path))


def _open_company_db() -> sqlite3.Connection:
    settings = _data_settings()
    try:
        return connect(settings.db_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def company_connection() -> Iterator[sqlite3.Connection]:
    connection = _open_company_db()
    try:
        yield connection
    finally:
        connection.close()


def news_connection() -> Iterator[sqlite3.Connection]:
    connection = open_news_db(_data_settings().news_db_path)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def _service_context() -> Iterator[NewsIntelligenceService]:
    settings = _data_settings()
    company_db = _open_company_db()
    news_db = None
    store = None
    try:
        news_db = open_news_db(settings.news_db_path)
        intel_settings = IntelSettings.from_env()
        intel_path = _intel_path()
        intel_path.parent.mkdir(parents=True, exist_ok=True)
        store = SqlStore.sqlite(intel_path)
        service = build_service(
            intel_settings,
            store=store,
            news=SqliteNewsRepository(news_db),
            companies=SqliteCompanyRepository(company_db),
            relationships=PaymentRelationshipRepository(company_db),
            analytics=BankReportsIndex(settings.reports_ocr_dir),
            calculations=PaymentCalculationRepository(company_db),
        )
        yield service
    finally:
        company_db.close()
        if news_db is not None:
            news_db.close()
        if store is not None:
            store.connection.close()


def intelligence_service() -> Iterator[NewsIntelligenceService]:
    try:
        with _service_context() as service:
            yield service
    except (ConfigError, FileNotFoundError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@lru_cache(maxsize=4)
def _reports_index(path: str) -> BankReportsIndex:
    return BankReportsIndex(path)


@app.get("/", include_in_schema=False)
def web_app() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/v1/health")
def health() -> dict:
    settings = _data_settings()
    intel = IntelSettings.from_env()
    index = _reports_index(str(settings.reports_ocr_dir))
    return {
        "status": "ok",
        "services": {
            "company_data": settings.db_path.exists(),
            "news_catalog": settings.news_db_path.exists(),
            "bank_reports": len(index.documents) > 0,
            "llm": intel.use_llm and bool(intel.llm_api_key),
        },
        "model": intel.llm_model if intel.use_llm else None,
    }


@app.get("/api/v1/data/report")
def data_report(conn: sqlite3.Connection = Depends(company_connection)) -> dict:
    return quality_report(conn)


@app.get("/api/v1/companies")
def companies(
    query: str | None = None,
    min_revenue: float | None = Query(default=15_000_000_000, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    conn: sqlite3.Connection = Depends(company_connection),
) -> dict:
    rows = SqliteCompanyRepository(conn).list_companies(
        min_revenue=min_revenue, query=query, limit=limit, offset=offset
    )
    return {"items": rows, "limit": limit, "offset": offset}


@app.get("/api/v1/companies/{company_id}")
def company_profile(company_id: str, conn: sqlite3.Connection = Depends(company_connection)) -> dict:
    profile = SqliteCompanyRepository(conn).get_company(company_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return profile.to_dict()


@app.get("/api/v1/companies/{company_id}/relationships")
def company_relationships(
    company_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    conn: sqlite3.Connection = Depends(company_connection),
) -> dict:
    repository = SqliteCompanyRepository(conn)
    if repository.get_company(company_id) is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    relationships = PaymentRelationshipRepository(conn).list_relationships(company_id, limit=limit)
    return {"items": [item.to_dict() for item in relationships]}


@app.get("/api/v1/companies/{company_id}/exposure")
def company_exposure(
    company_id: str,
    direction: str | None = Query(default=None, pattern="^(incoming|outgoing)$"),
    okved_filter: list[str] = Query(default=[]),
    conn: sqlite3.Connection = Depends(company_connection),
) -> dict:
    if okved_filter:
        if direction is None:
            return {
                key: calculate_sector_exposure(conn, company_id, okved_filter, key)
                for key in ("incoming", "outgoing")
            }
        return calculate_sector_exposure(conn, company_id, okved_filter, direction)
    result = get_counterparty_exposure(conn, company_id)
    if direction:
        result["data"] = {direction: result.get("data", {}).get(direction)}
    return result


@app.get("/api/v1/companies/{company_id}/calculations")
def company_calculations(company_id: str, conn: sqlite3.Connection = Depends(company_connection)) -> dict:
    if SqliteCompanyRepository(conn).get_company(company_id) is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return {"items": PaymentCalculationRepository(conn).list_calculations(company_id)}


@app.get("/api/v1/news/sources")
def news_sources() -> dict:
    return {
        "items": [
            {"source_id": source.source_id, "name": source.name, "note": source.note}
            for source in enabled_sources()
        ]
    }


@app.get("/api/v1/news/search")
def news_search(
    query: str = Query(min_length=2, max_length=300),
    days: int | None = Query(default=None, ge=1, le=3650),
    limit: int = Query(default=10, ge=1, le=50),
    prefilter: list[str] = Query(default=["KEEP", "REVIEW"]),
    conn: sqlite3.Connection = Depends(news_connection),
) -> dict:
    from datetime import datetime, timedelta, timezone

    if any(value not in {"KEEP", "REVIEW", "DROP"} for value in prefilter):
        raise HTTPException(status_code=422, detail="Недопустимый фильтр релевантности")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else None
    return search_news(conn, query, period=(since, None), filters={"prefilter": prefilter}, limit=limit)


@app.get("/api/v1/news")
def news_feed(
    prefilter: list[str] = Query(default=["KEEP", "REVIEW"]),
    source_id: str | None = None,
    include_duplicates: bool = False,
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    conn: sqlite3.Connection = Depends(news_connection),
) -> dict:
    allowed = {"KEEP", "REVIEW", "DROP"}
    if any(value not in allowed for value in prefilter):
        raise HTTPException(status_code=422, detail="Недопустимый фильтр релевантности")
    items = list_news(
        conn,
        prefilter=tuple(prefilter),
        source_id=source_id,
        include_duplicates=include_duplicates,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "limit": limit, "offset": offset}


@app.get("/api/v1/news/{news_id}")
def news_detail(news_id: str, conn: sqlite3.Connection = Depends(news_connection)) -> dict:
    record = SqliteNewsRepository(conn).get_news(news_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Новость не найдена")
    cluster_id = record.metadata.get("cluster_id") or news_id
    return {"news": record.to_dict(), "sources": cluster_sources(conn, cluster_id)}


@app.post("/api/v1/news", status_code=201)
def create_news(request: NewsCreateRequest, conn: sqlite3.Connection = Depends(news_connection)) -> dict:
    result = add_manual_text(
        conn,
        request.title,
        request.text,
        url=str(request.url) if request.url else None,
        published_at=request.published_at,
    )
    return asdict(result)


@app.post("/api/v1/news/fetch")
def fetch_news(request: NewsFetchRequest, conn: sqlite3.Connection = Depends(news_connection)) -> dict:
    if request.source_ids:
        sources = []
        for source_id in request.source_ids:
            source = source_by_id(source_id)
            if source is None or not source.enabled:
                raise HTTPException(status_code=422, detail=f"Источник недоступен: {source_id}")
            sources.append(source)
    else:
        sources = enabled_sources()
    return fetch_all(conn, sources=sources, max_items=request.max_items)


@app.get("/api/v1/reports")
def reports() -> dict:
    return {"items": [asdict(document) for document in DOCUMENTS]}


@app.get("/api/v1/reports/search")
def reports_search(
    query: str = Query(min_length=2, max_length=300),
    limit: int = Query(default=5, ge=1, le=20),
) -> dict:
    settings = _data_settings()
    index = _reports_index(str(settings.reports_ocr_dir))
    snippets = index.search(query, limit=limit)
    return {"items": [snippet.to_dict() for snippet in snippets], "query": query}


@app.post("/api/v1/analysis")
def analyze_company(
    request: AnalysisRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    _validate_analysis_request(request)
    job_id = uuid.uuid4().hex
    _job_store().create(job_id, "analysis", request.model_dump(mode="json"))
    background_tasks.add_task(_run_analysis_job, job_id)
    return {"job_id": job_id, "status": "queued", "status_url": f"/api/v1/jobs/{job_id}"}


def _validate_analysis_request(request: AnalysisRequest) -> None:
    company_db = _open_company_db()
    try:
        if SqliteCompanyRepository(company_db).get_company(request.company_id) is None:
            raise HTTPException(status_code=404, detail="Компания не найдена")
    finally:
        company_db.close()

    news_db = open_news_db(_data_settings().news_db_path)
    try:
        repository = SqliteNewsRepository(news_db)
        missing = [news_id for news_id in request.event_ids if repository.get_news(news_id) is None]
        if missing:
            raise HTTPException(
                status_code=404,
                detail={"message": "Новости не найдены", "news_ids": missing},
            )
    finally:
        news_db.close()


def _run_analysis_job(job_id: str) -> None:
    jobs = _job_store()
    job = jobs.get(job_id)
    if job is None:
        return
    payload = job["payload"]
    event_ids = payload["event_ids"]
    failed_news: list[str] = []
    processing_errors: list[str] = []
    try:
        jobs.update(job_id, status="running", stage="Подготовка данных", progress=0.03)
        with _service_context() as service:
            for index, news_id in enumerate(event_ids, start=1):
                jobs.update(
                    job_id,
                    status="running",
                    stage=f"Извлечение фактов · {index}/{len(event_ids)}",
                    progress=0.05 + 0.45 * (index - 1) / len(event_ids),
                )
                try:
                    processing = service.process_news(news_id)
                except IntelError as exc:
                    logger.warning("news_processing_failed news_id=%s code=%s", news_id, exc.code)
                    failed_news.append(news_id)
                    processing_errors.append(_public_error(exc))
                    continue
                if not processing.processed:
                    failed_news.append(news_id)
                    processing_errors.append(
                        f"Обработка новости завершилась статусом {processing.status.value}"
                    )
            successful_event_ids = [news_id for news_id in event_ids if news_id not in failed_news]
            if not successful_event_ids:
                jobs.update(
                    job_id,
                    status="failed",
                    stage="Факты не извлечены",
                    progress=1,
                    error=processing_errors[0] if processing_errors else "Не удалось извлечь факты",
                )
                return
            jobs.update(job_id, status="running", stage="Оценка влияния на компанию", progress=0.55)
            result = service.analyze_company_impact(payload["company_id"], successful_event_ids)
            serialized = result.to_dict()
            if failed_news:
                serialized["analysis_status"] = "partial"
                serialized.setdefault("limitations", []).append(
                    "Не удалось обработать новости: " + ", ".join(failed_news)
                )
            analysis_status = serialized.get("analysis_status")
            status = analysis_status if analysis_status in {"partial", "failed"} else "completed"
            jobs.update(
                job_id,
                status=status,
                stage="Готово" if status == "completed" else "Готово с ограничениями",
                progress=1,
                result=serialized,
                error=(serialized.get("summary") or "Анализ не построен") if status == "failed" else None,
            )
    except Exception as exc:
        logger.exception("analysis_job_failed job_id=%s", job_id)
        jobs.update(
            job_id,
            status="failed",
            stage="Анализ не завершён",
            progress=1,
            error=_public_error(exc),
        )


def _public_error(error: Exception) -> str:
    if isinstance(error, IntelError):
        return f"{error.code}: {error.message}"
    if isinstance(error, HTTPException):
        return str(error.detail)
    return f"Внутренняя ошибка: {type(error).__name__}"


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = _job_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    job.pop("payload", None)
    return job


@app.get("/api/v1/analysis/{analysis_id}")
def get_analysis(analysis_id: str, service: NewsIntelligenceService = Depends(intelligence_service)) -> dict:
    result = service.get_analysis(analysis_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Разбор не найден")
    return result.to_dict()


@app.post("/api/v1/analysis/{analysis_id}/refinements")
def refine_analysis(
    analysis_id: str,
    request: RefinementRequest,
    background_tasks: BackgroundTasks,
    service: NewsIntelligenceService = Depends(intelligence_service),
) -> dict:
    if service.get_analysis(analysis_id) is None:
        raise HTTPException(status_code=404, detail="Разбор не найден")
    job_id = uuid.uuid4().hex
    payload = {"analysis_id": analysis_id, **request.model_dump(mode="json")}
    _job_store().create(job_id, "refinement", payload)
    background_tasks.add_task(_run_refinement_job, job_id)
    return {"job_id": job_id, "status": "queued", "status_url": f"/api/v1/jobs/{job_id}"}


def _run_refinement_job(job_id: str) -> None:
    jobs = _job_store()
    job = jobs.get(job_id)
    if job is None:
        return
    payload = job["payload"]
    try:
        jobs.update(job_id, status="running", stage="Применение уточнения", progress=0.15)
        with _service_context() as service:
            result = service.adapt_company_analysis(
                payload["analysis_id"],
                text=payload["text"],
                kind=payload["kind"],
                affected_event_ids=payload["affected_event_ids"],
                horizon=payload["horizon"],
            )
        serialized = result.to_dict()
        analysis_status = serialized.get("analysis_status")
        status = analysis_status if analysis_status in {"partial", "failed"} else "completed"
        jobs.update(
            job_id,
            status=status,
            stage="Готово" if status == "completed" else "Готово с ограничениями",
            progress=1,
            result=serialized,
            error=(serialized.get("summary") or "Анализ не построен") if status == "failed" else None,
        )
    except Exception as exc:
        logger.exception("refinement_job_failed job_id=%s", job_id)
        jobs.update(job_id, status="failed", stage="Уточнение не применено", progress=1, error=_public_error(exc))


@app.get("/api/v1/processing/{news_id}")
def processing_result(
    news_id: str,
    service: NewsIntelligenceService = Depends(intelligence_service),
) -> dict:
    result = service.get_processing_result(news_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Обработка новости ещё не запускалась")
    return result.to_dict()
