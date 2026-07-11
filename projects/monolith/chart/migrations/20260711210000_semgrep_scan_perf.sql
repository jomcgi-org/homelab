-- semgrep.scan_perf: one row per Semgrep scan we track, for the private
-- Route B vs Semgrep Managed Scans performance comparison page.
-- Two writers: report.py inserts Route B rows at scan-complete (authoritative
-- runtime); a scheduled harvest upserts SMS rows from the Semgrep API.
CREATE SCHEMA IF NOT EXISTS semgrep;

CREATE TABLE IF NOT EXISTS semgrep.scan_perf (
    id                 BIGSERIAL PRIMARY KEY,
    scan_id            BIGINT NOT NULL UNIQUE,
    environment        TEXT NOT NULL DEFAULT '',
    raw_environment    TEXT NOT NULL DEFAULT '',
    is_full_scan       BOOLEAN NOT NULL DEFAULT FALSE,
    branch             TEXT NOT NULL DEFAULT '',
    scan_ref           TEXT NOT NULL DEFAULT '',
    commit_sha         TEXT NOT NULL DEFAULT '',
    total_time         DOUBLE PRECISION NOT NULL DEFAULT 0,
    findings_total     INTEGER NOT NULL DEFAULT 0,
    cli_version        TEXT NOT NULL DEFAULT '',
    scan_started_at    TIMESTAMPTZ,
    scan_completed_at  TIMESTAMPTZ,
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT scan_perf_environment_valid
        CHECK (environment IN ('route-b', 'managed-scans', ''))
);

CREATE INDEX IF NOT EXISTS scan_perf_commit ON semgrep.scan_perf (commit_sha);
CREATE INDEX IF NOT EXISTS scan_perf_ref ON semgrep.scan_perf (scan_ref);
CREATE INDEX IF NOT EXISTS scan_perf_env_full_completed
    ON semgrep.scan_perf (environment, is_full_scan, scan_completed_at DESC);
