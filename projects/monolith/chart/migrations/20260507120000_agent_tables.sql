-- Adds tables for the agent MCP surface (v1):
--   routine_jobs  - delegated work claimed and run by cloud Routines
--                   (the claude-routine-agent actor). Distinct from
--                   scheduled_jobs which is owned by the in-cluster
--                   tick loop; these tables share no rows.
--   agent_locks   - opportunistic TTL locks for ad-hoc dedup keyed
--                   by free-form string.
--
-- The `attempts` column on routine_jobs is intentionally unused by
-- v1 code. It is pre-deployed so v2's planned priority calculation
-- (which counts how many times a gap has been picked up) does not
-- require an online migration against a populated table.

CREATE TABLE routine_jobs (
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
    ON routine_jobs (next_run_at)
    WHERE locked_by IS NULL;

CREATE INDEX idx_routine_jobs_kind
    ON routine_jobs (routine_kind);

CREATE TABLE agent_locks (
    key          TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    acquired_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    lock_id      UUID NOT NULL DEFAULT gen_random_uuid()
);

CREATE INDEX idx_agent_locks_expires
    ON agent_locks (expires_at);
