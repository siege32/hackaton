-- Additive migration for the AI news-intelligence module.
-- Safe to run repeatedly. Does NOT alter tables owned by other teams.
-- Compatible with SQLite and PostgreSQL (TEXT/JSON stored as TEXT).

CREATE TABLE IF NOT EXISTS intel_news_processing (
    news_id             TEXT PRIMARY KEY,
    content_version     TEXT NOT NULL,
    status              TEXT NOT NULL,
    general_relevance   TEXT NOT NULL,
    result_json         TEXT NOT NULL,
    source_id           TEXT,
    model_id            TEXT,
    prompt_version      TEXT,
    processing_version  TEXT,
    job_id              TEXT,
    error_code          TEXT,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intel_news_facts (
    fact_id         TEXT PRIMARY KEY,
    news_id         TEXT NOT NULL,
    content_version TEXT NOT NULL,
    source_id       TEXT,
    fact_json       TEXT NOT NULL,
    quote           TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_facts_news
    ON intel_news_facts (news_id, content_version);

CREATE TABLE IF NOT EXISTS intel_company_assessments (
    news_id             TEXT NOT NULL,
    company_id          TEXT NOT NULL,
    assessment_id       TEXT NOT NULL,
    content_version     TEXT NOT NULL,
    profile_version     TEXT NOT NULL,
    relevance_class     TEXT NOT NULL,
    impact_direction    TEXT NOT NULL,
    impact_type         TEXT NOT NULL,
    model_id            TEXT,
    prompt_version      TEXT,
    processing_version  TEXT,
    result_json         TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (news_id, company_id)
);

CREATE INDEX IF NOT EXISTS idx_intel_assess_company
    ON intel_company_assessments (company_id, relevance_class);

CREATE TABLE IF NOT EXISTS intel_company_assessment_history (
    history_id          TEXT PRIMARY KEY,
    assessment_id       TEXT NOT NULL,
    news_id             TEXT NOT NULL,
    company_id          TEXT NOT NULL,
    content_version     TEXT NOT NULL,
    profile_version     TEXT NOT NULL,
    relevance_class     TEXT NOT NULL,
    result_json         TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intel_insight_analyses (
    analysis_id             TEXT PRIMARY KEY,
    company_id              TEXT NOT NULL,
    analysis_status         TEXT NOT NULL,
    company_profile_version TEXT,
    parent_analysis_id      TEXT,
    result_json             TEXT NOT NULL,
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_analysis_company
    ON intel_insight_analyses (company_id, created_at);

CREATE TABLE IF NOT EXISTS intel_refinements (
    refinement_id TEXT PRIMARY KEY,
    analysis_id   TEXT NOT NULL,
    company_id    TEXT NOT NULL,
    result_json   TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_refinements_analysis
    ON intel_refinements (analysis_id);
