"""Analytics database schema — PostgreSQL-idiomatic design, SQLite runtime.

Designed to be trivially portable to real PostgreSQL (change connection string).
Uses TEXT over VARCHAR, REAL over FLOAT for SQLite compat; all other PG patterns
(integer PKs, foreign keys, indexes, views, materialized aggregates) are standard.
"""

import os
from pathlib import Path

from applypilot.config import APP_DIR

DB_PATH = APP_DIR / "analytics.db"

SCHEMA_SQL = """
-- Core table: one row-per-job-attempt, enriched from log parsing
CREATE TABLE IF NOT EXISTS job_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_url         TEXT NOT NULL,
    job_title       TEXT,
    company         TEXT,
    ats_type        TEXT,            -- Workday, Greenhouse, Lever, Taleo, etc.
    attempt_date    TEXT,            -- ISO date of the attempt
    attempt_start   TEXT,            -- ISO timestamp
    attempt_end     TEXT,            -- ISO timestamp
    duration_ms     INTEGER,
    provider        TEXT,            -- opencode-zen, openrouter, etc.
    model           TEXT,            -- nemotron-3-super-free, deepseek-v4-flash
    base_url        TEXT,            -- API endpoint used
    result          TEXT,            -- applied, failed:stuck, expired, etc.
    result_reason   TEXT,            -- agent's detailed failure reason
    session_id      TEXT,            -- Hermes session id
    log_file_path   TEXT,            -- path to the claude_*.txt transcript
    
    -- Rolled-up API metrics (computed during parse)
    total_api_calls      INTEGER DEFAULT 0,
    total_prompt_tokens  INTEGER DEFAULT 0,
    total_completion_tokens INTEGER DEFAULT 0,
    total_cached_tokens  INTEGER DEFAULT 0,
    total_latency_ms     INTEGER DEFAULT 0,
    avg_latency_ms       REAL DEFAULT 0.0,
    
    -- Rolled-up tool metrics
    total_tool_calls     INTEGER DEFAULT 0,
    total_tool_errors    INTEGER DEFAULT 0,
    total_tool_duration_ms INTEGER DEFAULT 0
);

-- Indexes for the queries we run most often
CREATE INDEX IF NOT EXISTS idx_attempts_result ON job_attempts(result);
CREATE INDEX IF NOT EXISTS idx_attempts_model  ON job_attempts(model);
CREATE INDEX IF NOT EXISTS idx_attempts_ats    ON job_attempts(ats_type);
CREATE INDEX IF NOT EXISTS idx_attempts_date   ON job_attempts(attempt_date);
CREATE INDEX IF NOT EXISTS idx_attempts_company ON job_attempts(company);

-- Unique constraint: prevent re-parsing the same log file
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_logpath ON job_attempts(log_file_path);

-- One row per API call made during a job attempt
CREATE TABLE IF NOT EXISTS api_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id       INTEGER NOT NULL REFERENCES job_attempts(id) ON DELETE CASCADE,
    call_number      INTEGER NOT NULL,   -- API call #N within this attempt
    model            TEXT,
    prompt_tokens    INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens     INTEGER DEFAULT 0,
    cached_tokens    INTEGER DEFAULT 0,
    latency_ms       INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cache_hit_pct    REAL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_apicalls_attempt ON api_calls(attempt_id);

-- One row per tool call during a job attempt
CREATE TABLE IF NOT EXISTS tool_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id       INTEGER NOT NULL REFERENCES job_attempts(id) ON DELETE CASCADE,
    call_number      INTEGER NOT NULL,
    tool_name        TEXT NOT NULL,
    success          INTEGER DEFAULT 1,  -- 1 = success, 0 = error
    duration_ms      INTEGER DEFAULT 0,
    result_size_chars INTEGER DEFAULT 0,
    error_type       TEXT,   -- parameter_error, timeout, css_selector, runtime, etc.
    error_snippet    TEXT    -- first 200 chars of the error
);

CREATE INDEX IF NOT EXISTS idx_toolcalls_attempt ON tool_calls(attempt_id);
CREATE INDEX IF NOT EXISTS idx_toolcalls_name    ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_toolcalls_success ON tool_calls(success);

-- Materialized model-performance summary view
CREATE VIEW IF NOT EXISTS model_performance AS
SELECT
    ja.model,
    ja.provider,
    COUNT(*)                                              AS attempts,
    SUM(CASE WHEN ja.result = 'applied' THEN 1 ELSE 0 END) AS applied,
    ROUND(100.0 * SUM(CASE WHEN ja.result = 'applied' THEN 1 ELSE 0 END)
          / NULLIF(COUNT(*), 0), 1)                       AS success_pct,
    ROUND(AVG(ja.duration_ms) / 1000.0, 0)                AS avg_duration_s,
    ROUND(AVG(ja.total_api_calls), 0)                     AS avg_api_calls,
    ROUND(AVG(ja.total_tool_errors), 1)                   AS avg_tool_errors,
    ROUND(AVG(ja.avg_latency_ms), 1)                      AS avg_api_latency_ms,
    ROUND(AVG(ja.total_prompt_tokens + ja.total_completion_tokens), 0) AS avg_tokens
FROM job_attempts ja
GROUP BY ja.model, ja.provider;

-- Materialized ATS-performance summary view
CREATE VIEW IF NOT EXISTS ats_performance AS
SELECT
    ja.ats_type,
    COUNT(*)                                              AS attempts,
    SUM(CASE WHEN ja.result = 'applied' THEN 1 ELSE 0 END) AS applied,
    ROUND(100.0 * SUM(CASE WHEN ja.result = 'applied' THEN 1 ELSE 0 END)
          / NULLIF(COUNT(*), 0), 1)                       AS success_pct,
    ROUND(AVG(ja.duration_ms) / 1000.0, 0)                AS avg_duration_s,
    ROUND(AVG(ja.total_tool_errors), 1)                   AS avg_tool_errors,
    ROUND(AVG(ja.total_api_calls), 0)                     AS avg_api_calls,
    ja.model
FROM job_attempts ja
WHERE ja.ats_type IS NOT NULL
GROUP BY ja.ats_type, ja.model;

-- Error-pattern analysis view
CREATE VIEW IF NOT EXISTS error_analysis AS
SELECT
    tc.error_type,
    tc.tool_name,
    ja.model,
    COUNT(*)                                              AS occurrences,
    ROUND(AVG(tc.duration_ms) / 1000.0, 1)                AS avg_wasted_s
FROM tool_calls tc
JOIN job_attempts ja ON ja.id = tc.attempt_id
WHERE tc.success = 0
GROUP BY tc.error_type, tc.tool_name, ja.model
ORDER BY occurrences DESC;

-- Tool-usage pattern view (what tools each model uses most)
CREATE VIEW IF NOT EXISTS tool_usage AS
SELECT
    tc.tool_name,
    ja.model,
    COUNT(*)                                              AS calls,
    ROUND(100.0 * SUM(CASE WHEN tc.success THEN 1 ELSE 0 END)
          / NULLIF(COUNT(*), 0), 1)                       AS success_pct,
    ROUND(AVG(tc.duration_ms), 1)                         AS avg_duration_ms
FROM tool_calls tc
JOIN job_attempts ja ON ja.id = tc.attempt_id
GROUP BY tc.tool_name, ja.model
ORDER BY calls DESC;
"""


def init_db(db_path: str | Path | None = None) -> str:
    """Create/upgrade the analytics database. Returns the path."""
    import sqlite3

    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    print(f"[analytics] Database ready: {path}")
    return str(path)
