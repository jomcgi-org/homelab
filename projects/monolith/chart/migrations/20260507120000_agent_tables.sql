-- Adds the claude_agent schema and its tables for the agent MCP surface (v1):
--   claude_agent.routine_jobs  - delegated work claimed and run by cloud Routines
--                                (the claude-routine-agent actor). Distinct from
--                                scheduler.scheduled_jobs which is owned by the
--                                in-cluster tick loop; these tables share no rows.
--   claude_agent.agent_locks   - opportunistic TTL locks for ad-hoc dedup keyed
--                                by free-form string.
--
-- Schema isolation: the claude_agent schema gives the agent surface its own
-- namespace independent of public and scheduler. This makes the boundary
-- between agent-owned and cluster-owned data explicit, and leaves room for
-- a future per-schema GRANT if we ever scope DB access by role.
--
-- The `attempts` column on routine_jobs is intentionally unused by v1 code.
-- It is pre-deployed so v2's planned priority calculation (which counts how
-- many times a gap has been picked up) does not require an online migration
-- against a populated table.

CREATE SCHEMA IF NOT EXISTS claude_agent;

CREATE TABLE claude_agent.routine_jobs (
    name             TEXT PRIMARY KEY,
    routine_kind     TEXT NOT NULL,
    interval_secs    INTEGER,
    next_run_at      TIMESTAMPTZ,
    last_run_at      TIMESTAMPTZ,
    last_status      TEXT,
    last_summary     TEXT,
    locked_by        TEXT,
    locked_at        TIMESTAMPTZ,
    ttl_secs         INTEGER,
    payload          JSONB,
    created_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts         INTEGER NOT NULL DEFAULT 0  -- reserved for v2 priority calc; v1 does not read or write
);

CREATE INDEX idx_routine_jobs_due
    ON claude_agent.routine_jobs (next_run_at)
    WHERE locked_by IS NULL;

CREATE INDEX idx_routine_jobs_kind
    ON claude_agent.routine_jobs (routine_kind);

CREATE TABLE claude_agent.agent_locks (
    key          TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    acquired_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    lock_id      UUID NOT NULL DEFAULT gen_random_uuid()
);

CREATE INDEX idx_agent_locks_expires
    ON claude_agent.agent_locks (expires_at);
