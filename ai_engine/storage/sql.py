"""SQL adapters (DB-API 2.0).

Default target is SQLite (used in tests); PostgreSQL works with ``%s``
placeholders and ``ON CONFLICT``.  The module only creates ``intel_*`` tables
and reads the news/company tables through configurable column mappings.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..errors import StorageError
from ..models import (
    CompanyNewsAssessment,
    CompanyProfile,
    InsightResult,
    NewsFact,
    NewsProcessingResult,
    NewsRecord,
    ProcessingStatus,
    Refinement,
    Relationship,
)
from ..utils import stable_id, utcnow_iso
from .base import AnalyticsSnippet

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(value: str) -> str:
    if not _IDENT_RE.match(value or ""):
        raise StorageError(f"unsafe SQL identifier: {value!r}")
    return value


def _row_dict(cursor, row) -> dict[str, Any]:
    names = [desc[0] for desc in cursor.description or []]
    return dict(zip(names, tuple(row)))


def _maybe_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return default
        return [stripped] if default.__class__ is list else default
    return default


class SqlStore:
    """Persists the module's own additive tables."""

    def __init__(self, connection, *, placeholder: str | None = None) -> None:
        self.connection = connection
        module = type(connection).__module__.split(".")[0]
        self.placeholder = placeholder or ("%s" if module in {"psycopg", "psycopg2", "pg8000"} else "?")

    @classmethod
    def sqlite(cls, path: str | Path) -> "SqlStore":
        connection = sqlite3.connect(str(path))
        connection.row_factory = sqlite3.Row
        store = cls(connection, placeholder="?")
        store.ensure_schema()
        return store

    def ensure_schema(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        raw = schema_path.read_text(encoding="utf-8")
        without_comments = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("--")
        )
        statements = [s.strip() for s in without_comments.split(";") if s.strip()]
        try:
            cursor = self.connection.cursor()
            for statement in statements:
                cursor.execute(statement)
            self.connection.commit()
        except Exception as exc:
            self._rollback()
            raise StorageError(f"failed to apply intel schema: {exc}") from exc

    # ------------------------------------------------------------------
    @contextmanager
    def _tx(self, operation: str) -> Iterator[Any]:
        cursor = self.connection.cursor()
        try:
            yield cursor
            self.connection.commit()
        except Exception as exc:
            self._rollback()
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"database error during {operation}: {exc}") from exc
        finally:
            try:
                cursor.close()
            except Exception:  # pragma: no cover
                pass

    def _rollback(self) -> None:
        try:
            self.connection.rollback()
        except Exception:  # pragma: no cover
            pass

    # -- news processing -------------------------------------------------
    def begin_processing(
        self,
        news_id: str,
        content_version: str,
        processing_version: str,
        job_id: str | None = None,
    ) -> None:
        result = NewsProcessingResult(
            news_id=news_id,
            content_version=content_version,
            status=ProcessingStatus.PROCESSING,
            classification_reason="processing in progress",
            processing_version=processing_version,
            job_id=job_id,
            updated_at=utcnow_iso(),
        )
        self._upsert_processing(result)

    def save_processing_result(self, result: NewsProcessingResult) -> None:
        with self._tx("save_processing_result") as cursor:
            cursor.execute(
                f"DELETE FROM intel_news_facts WHERE news_id = {self.placeholder}",
                (result.news_id,),
            )
            for fact in result.facts:
                cursor.execute(
                    f"""INSERT INTO intel_news_facts
                        (fact_id, news_id, content_version, source_id, fact_json, quote, created_at)
                        VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder},
                                {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder})""",
                    (
                        fact.fact_id,
                        fact.news_id,
                        fact.content_version,
                        fact.source_id,
                        json.dumps(fact.to_dict(), ensure_ascii=False),
                        fact.quote,
                        fact.created_at,
                    ),
                )
            self._write_processing(cursor, result)

    def _upsert_processing(self, result: NewsProcessingResult) -> None:
        with self._tx("save_processing") as cursor:
            self._write_processing(cursor, result)

    def _write_processing(self, cursor, result: NewsProcessingResult) -> None:
        p = self.placeholder
        cursor.execute(
            f"""INSERT INTO intel_news_processing
                (news_id, content_version, status, general_relevance, result_json,
                 source_id, model_id, prompt_version, processing_version, job_id,
                 error_code, updated_at)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})
                ON CONFLICT(news_id) DO UPDATE SET
                    content_version=excluded.content_version,
                    status=excluded.status,
                    general_relevance=excluded.general_relevance,
                    result_json=excluded.result_json,
                    source_id=excluded.source_id,
                    model_id=excluded.model_id,
                    prompt_version=excluded.prompt_version,
                    processing_version=excluded.processing_version,
                    job_id=excluded.job_id,
                    error_code=excluded.error_code,
                    updated_at=excluded.updated_at""",
            (
                result.news_id,
                result.content_version,
                result.status.value,
                result.general_relevance.value,
                json.dumps(result.to_dict(), ensure_ascii=False),
                result.source_id,
                result.model_id,
                result.prompt_version,
                result.processing_version,
                result.job_id,
                result.error_code,
                result.updated_at,
            ),
        )

    def get_processing_result(self, news_id: str) -> NewsProcessingResult | None:
        try:
            cursor = self.connection.cursor()
            cursor.execute(
                f"SELECT result_json FROM intel_news_processing WHERE news_id = {self.placeholder}",
                (news_id,),
            )
            row = cursor.fetchone()
            cursor.close()
        except Exception as exc:
            raise StorageError(f"failed to load processing result: {exc}") from exc
        if not row:
            return None
        try:
            return NewsProcessingResult.from_dict(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            raise StorageError(f"corrupted processing result for news {news_id}") from exc

    def get_facts(self, news_id: str, content_version: str | None = None) -> list[NewsFact]:
        query = (
            f"SELECT fact_json FROM intel_news_facts WHERE news_id = {self.placeholder}"
        )
        params: list[Any] = [news_id]
        if content_version is not None:
            query += f" AND content_version = {self.placeholder}"
            params.append(content_version)
        try:
            cursor = self.connection.cursor()
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            cursor.close()
        except Exception as exc:
            raise StorageError(f"failed to load facts: {exc}") from exc
        facts: list[NewsFact] = []
        for row in rows:
            try:
                facts.append(NewsFact.from_dict(json.loads(row[0])))
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                raise StorageError(f"corrupted fact row for news {news_id}") from exc
        return facts

    # -- assessments -----------------------------------------------------
    def save_assessment(self, assessment: CompanyNewsAssessment) -> None:
        p = self.placeholder
        payload = json.dumps(assessment.to_dict(), ensure_ascii=False)
        with self._tx("save_assessment") as cursor:
            cursor.execute(
                f"""INSERT INTO intel_company_assessments
                    (news_id, company_id, assessment_id, content_version, profile_version,
                     relevance_class, impact_direction, impact_type, model_id,
                     prompt_version, processing_version, result_json, created_at, updated_at)
                    VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})
                    ON CONFLICT(news_id, company_id) DO UPDATE SET
                        assessment_id=excluded.assessment_id,
                        content_version=excluded.content_version,
                        profile_version=excluded.profile_version,
                        relevance_class=excluded.relevance_class,
                        impact_direction=excluded.impact_direction,
                        impact_type=excluded.impact_type,
                        model_id=excluded.model_id,
                        prompt_version=excluded.prompt_version,
                        processing_version=excluded.processing_version,
                        result_json=excluded.result_json,
                        updated_at=excluded.updated_at""",
                (
                    assessment.news_id,
                    assessment.company_id,
                    assessment.assessment_id,
                    assessment.content_version,
                    assessment.profile_version,
                    assessment.relevance_class.value,
                    assessment.impact_direction.value,
                    assessment.impact_type.value,
                    assessment.model_id,
                    assessment.prompt_version,
                    assessment.processing_version,
                    payload,
                    assessment.created_at,
                    assessment.updated_at,
                ),
            )
            history_id = f"assesshist_{uuid.uuid4().hex}"
            cursor.execute(
                f"""INSERT INTO intel_company_assessment_history
                    (history_id, assessment_id, news_id, company_id, content_version,
                     profile_version, relevance_class, result_json, created_at)
                    VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})""",
                (
                    history_id,
                    assessment.assessment_id,
                    assessment.news_id,
                    assessment.company_id,
                    assessment.content_version,
                    assessment.profile_version,
                    assessment.relevance_class.value,
                    payload,
                    assessment.created_at,
                ),
            )

    def get_assessment(self, news_id: str, company_id: str) -> CompanyNewsAssessment | None:
        p = self.placeholder
        try:
            cursor = self.connection.cursor()
            cursor.execute(
                f"""SELECT result_json FROM intel_company_assessments
                    WHERE news_id = {p} AND company_id = {p}""",
                (news_id, company_id),
            )
            row = cursor.fetchone()
            cursor.close()
        except Exception as exc:
            raise StorageError(f"failed to load assessment: {exc}") from exc
        if not row:
            return None
        try:
            return CompanyNewsAssessment.from_dict(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            raise StorageError("corrupted assessment row") from exc

    def list_assessments_for_news(self, news_id: str) -> list[CompanyNewsAssessment]:
        return self._list_assessments("news_id", news_id)

    def list_assessments_for_company(self, company_id: str) -> list[CompanyNewsAssessment]:
        return self._list_assessments("company_id", company_id)

    def _list_assessments(self, field: str, value: str) -> list[CompanyNewsAssessment]:
        _validate_identifier(field)
        try:
            cursor = self.connection.cursor()
            cursor.execute(
                f"SELECT result_json FROM intel_company_assessments WHERE {field} = {self.placeholder}",
                (value,),
            )
            rows = cursor.fetchall()
            cursor.close()
        except Exception as exc:
            raise StorageError(f"failed to list assessments: {exc}") from exc
        results = []
        for row in rows:
            try:
                results.append(CompanyNewsAssessment.from_dict(json.loads(row[0])))
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                raise StorageError("corrupted assessment row") from exc
        return results

    # -- insight analyses ------------------------------------------------
    def save_analysis(self, analysis: InsightResult) -> None:
        p = self.placeholder
        with self._tx("save_analysis") as cursor:
            cursor.execute(
                f"""INSERT INTO intel_insight_analyses
                    (analysis_id, company_id, analysis_status, company_profile_version,
                     parent_analysis_id, result_json, created_at)
                    VALUES ({p},{p},{p},{p},{p},{p},{p})
                    ON CONFLICT(analysis_id) DO UPDATE SET
                        analysis_status=excluded.analysis_status,
                        result_json=excluded.result_json""",
                (
                    analysis.analysis_id,
                    analysis.company_id,
                    analysis.analysis_status.value,
                    analysis.company_profile_version,
                    analysis.parent_analysis_id,
                    json.dumps(analysis.to_dict(), ensure_ascii=False),
                    analysis.created_at,
                ),
            )

    def get_analysis(self, analysis_id: str) -> InsightResult | None:
        try:
            cursor = self.connection.cursor()
            cursor.execute(
                f"SELECT result_json FROM intel_insight_analyses WHERE analysis_id = {self.placeholder}",
                (analysis_id,),
            )
            row = cursor.fetchone()
            cursor.close()
        except Exception as exc:
            raise StorageError(f"failed to load analysis: {exc}") from exc
        if not row:
            return None
        try:
            return InsightResult.from_dict(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            raise StorageError("corrupted analysis row") from exc

    def save_refinement(self, refinement: Refinement) -> None:
        p = self.placeholder
        with self._tx("save_refinement") as cursor:
            cursor.execute(
                f"""INSERT INTO intel_refinements
                    (refinement_id, analysis_id, company_id, result_json, created_at)
                    VALUES ({p},{p},{p},{p},{p})
                    ON CONFLICT(refinement_id) DO UPDATE SET result_json=excluded.result_json""",
                (
                    refinement.refinement_id,
                    refinement.analysis_id,
                    refinement.company_id,
                    json.dumps(refinement.to_dict(), ensure_ascii=False),
                    refinement.created_at,
                ),
            )

    def list_refinements(self, analysis_id: str) -> list[Refinement]:
        try:
            cursor = self.connection.cursor()
            cursor.execute(
                f"""SELECT result_json FROM intel_refinements
                    WHERE analysis_id = {self.placeholder} ORDER BY created_at""",
                (analysis_id,),
            )
            rows = cursor.fetchall()
            cursor.close()
        except Exception as exc:
            raise StorageError(f"failed to load refinements: {exc}") from exc
        results = []
        for row in rows:
            try:
                results.append(Refinement.from_dict(json.loads(row[0])))
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                raise StorageError("corrupted refinement row") from exc
        return results


class SqlNewsRepository:
    """Read-only adapter for the news table owned by the collection team."""

    DEFAULT_COLUMNS = {
        "news_id": "id",
        "title": "title",
        "text": "text",
        "source_id": "source_id",
        "content_version": "content_version",
        "published_at": "published_at",
        "url": "url",
        "language": "language",
    }

    def __init__(self, connection, *, table: str = "news", columns: dict[str, str] | None = None) -> None:
        self.connection = connection
        self.table = _validate_identifier(table)
        self.columns = {**self.DEFAULT_COLUMNS, **(columns or {})}
        for name in self.columns.values():
            _validate_identifier(name)
        self._placeholder = "%s" if type(connection).__module__.split(".")[0] in {
            "psycopg", "psycopg2", "pg8000",
        } else "?"

    def get_news(self, news_id: str) -> NewsRecord | None:
        try:
            cursor = self.connection.cursor()
            cursor.execute(
                f"SELECT * FROM {self.table} WHERE {self.columns['news_id']} = {self._placeholder}",
                (news_id,),
            )
            row = cursor.fetchone()
            row_dict = _row_dict(cursor, row) if row is not None else None
            cursor.close()
        except Exception as exc:
            raise StorageError(
                f"failed to read news from {self.table}: {exc}. "
                "Configure INTEL_NEWS_TABLE/columns for the real schema."
            ) from exc
        if row_dict is None:
            return None
        col = self.columns

        def get(name: str, default: Any = None) -> Any:
            return row_dict.get(col[name], default)

        return NewsRecord(
            news_id=str(get("news_id")),
            title=str(get("title") or ""),
            text=str(get("text") or ""),
            source_id=str(get("source_id") or ""),
            content_version=str(get("content_version") or ""),
            published_at=get("published_at"),
            url=get("url"),
            language=get("language"),
        )


class SqlCompanyRepository:
    """Read-only adapter for a company profile table.

    Column mapping is configurable because the real schema is owned by the
    news/data team.  JSON columns may be stored as JSON text or native arrays.
    """

    DEFAULT_COLUMNS = {
        "company_id": "id",
        "name": "name",
        "industry": "industry",
        "subindustry": "subindustry",
        "activities": "activities",
        "products_services": "products_services",
        "processes": "processes",
        "key_resources": "key_resources",
        "cost_drivers": "cost_drivers",
        "geography": "geography",
        "suppliers": "suppliers",
        "customers": "customers",
        "financials": "financials",
        "known_risks": "known_risks",
        "data_sources": "data_sources",
        "updated_at": "updated_at",
        "profile_version": "profile_version",
    }

    def __init__(self, connection, *, table: str = "companies", columns: dict[str, str] | None = None) -> None:
        self.connection = connection
        self.table = _validate_identifier(table)
        self.columns = {**self.DEFAULT_COLUMNS, **(columns or {})}
        for name in self.columns.values():
            _validate_identifier(name)
        self._placeholder = "%s" if type(connection).__module__.split(".")[0] in {
            "psycopg", "psycopg2", "pg8000",
        } else "?"

    def get_company(self, company_id: str) -> CompanyProfile | None:
        try:
            cursor = self.connection.cursor()
            cursor.execute(
                f"SELECT * FROM {self.table} WHERE {self.columns['company_id']} = {self._placeholder}",
                (company_id,),
            )
            row = cursor.fetchone()
            row_dict = _row_dict(cursor, row) if row is not None else None
            cursor.close()
        except Exception as exc:
            raise StorageError(
                f"failed to read company from {self.table}: {exc}. "
                "Configure INTEL_COMPANY_TABLE/columns for the real schema."
            ) from exc
        if row_dict is None:
            return None
        col = self.columns

        def get(name: str, default: Any = None) -> Any:
            return row_dict.get(col[name], default)

        def list_field(name: str) -> list:
            value = _maybe_json(get(name), [])
            return [str(v) for v in value] if isinstance(value, list) else []

        def dict_field(name: str) -> dict:
            value = _maybe_json(get(name), {})
            return value if isinstance(value, dict) else {}

        suppliers = _maybe_json(get("suppliers"), [])
        customers = _maybe_json(get("customers"), [])
        data_sources = _maybe_json(get("data_sources"), [])
        return CompanyProfile(
            company_id=str(get("company_id")),
            name=str(get("name") or ""),
            industry=get("industry"),
            subindustry=get("subindustry"),
            activities=list_field("activities"),
            products_services=list_field("products_services"),
            processes=list_field("processes"),
            key_resources=list_field("key_resources"),
            cost_drivers=list_field("cost_drivers"),
            geography=list_field("geography"),
            suppliers=[s for s in suppliers if isinstance(s, dict)],
            customers=[c for c in customers if isinstance(c, dict)],
            financials=dict_field("financials"),
            known_risks=list_field("known_risks"),
            data_sources=[s for s in data_sources if isinstance(s, dict)],
            updated_at=get("updated_at"),
            profile_version=str(get("profile_version") or ""),
        )


class SqlRelationshipRepository:
    """Read-only adapter for a counterparty/relationships table.

    If the real system stores relationships differently, implement the
    ``RelationshipRepository`` protocol instead; this adapter is a convenience
    for a conventional schema.
    """

    DEFAULT_COLUMNS = {
        "relationship_id": "relationship_id",
        "company_id": "company_id",
        "counterparty_id": "counterparty_id",
        "counterparty_name": "counterparty_name",
        "relation_type": "relation_type",
        "confirmed": "confirmed",
        "significance": "significance",
        "direction": "direction",
        "share": "share",
        "evidence": "evidence",
        "valid_from": "valid_from",
        "valid_until": "valid_until",
        "source": "source",
        "updated_at": "updated_at",
    }

    def __init__(self, connection, *, table: str = "company_relationships", columns: dict[str, str] | None = None) -> None:
        self.connection = connection
        self.table = _validate_identifier(table)
        self.columns = {**self.DEFAULT_COLUMNS, **(columns or {})}
        for name in self.columns.values():
            _validate_identifier(name)
        self._placeholder = "%s" if type(connection).__module__.split(".")[0] in {
            "psycopg", "psycopg2", "pg8000",
        } else "?"

    def list_relationships(self, company_id: str, limit: int = 50) -> list[Relationship]:
        try:
            cursor = self.connection.cursor()
            cursor.execute(
                f"SELECT * FROM {self.table} WHERE {self.columns['company_id']} = {self._placeholder} LIMIT {int(limit)}",
                (company_id,),
            )
            rows = cursor.fetchall()
            dicts = [_row_dict(cursor, row) for row in rows]
            cursor.close()
        except Exception as exc:
            raise StorageError(f"failed to read relationships: {exc}") from exc
        results = []
        for row in dicts:
            data = {logical: row.get(column) for logical, column in self.columns.items()}
            data["confirmed"] = str(data.get("confirmed", "")).lower() in {"1", "true", "yes", "t"}
            data["evidence"] = _maybe_json(data.get("evidence"), [])
            results.append(Relationship.from_dict(data))
        return results


class UnavailableAnalyticsIndex:
    """Explicit stub: banking analytics is not wired yet.

    It never pretends a search succeeded: callers receive a ``ToolError`` and
    the insight engine records the missing tool in ``limitations``.
    """

    def search(self, query: str, limit: int = 3) -> list[AnalyticsSnippet]:
        from ..errors import ToolUnavailableError

        raise ToolUnavailableError(
            "Banking analytics index is not configured. "
            "Implement AnalyticsIndex against the prepared index owned by the data team."
        )
