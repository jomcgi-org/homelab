-- sandbox.session: maps a caller-chosen handle to EmberVM session credentials
-- (EmberVM R2, ADR embervm/001; sessioned run_python, plan Task 10). The
-- monolith's run_python tool takes an optional opaque `session` handle; the
-- first use with a handle creates an EmberVM session (POST
-- /v1/workloads/sandbox-session/sessions) and stores its id + capability token
-- here, and later uses look the row up to reuse the same live/banked session so
-- variables, files, and warm imports accrete across an agent's turns.
--
-- token IS A SECRET: it is the per-session capability that authenticates every
-- invoke to this session (verified by hash, constant-time, on the EmberVM
-- side). It is never logged by the client and this table is PRIVATE-TIER ONLY:
-- no public_reader GRANT lands here, and nothing on the public origin reads
-- sandbox.session. A leaked token grants invoke access to exactly one session
-- (bounded by its max lifetime), nothing more.
--
-- expires_at mirrors the EmberVM session's max-lifetime deadline; a row whose
-- session has 410'd (expired/evicted/failed) is transparently replaced on the
-- next use (last-write-wins by handle), so stale rows are self-healing and no
-- separate reaper is required.
--
-- Private-tier schema: the private `app` role needs no explicit GRANT here
-- (mirrors 20260712000000_home_cluster_snapshot.sql and
-- 20260714000000_faas_function.sql).

CREATE SCHEMA IF NOT EXISTS sandbox;

CREATE TABLE sandbox.session (
    handle     TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    token      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

COMMENT ON COLUMN sandbox.session.token IS
    'SECRET: EmberVM per-session capability token. Never log it; private-tier only.';
