-- faas.function: the durable registry for EmberVM zip-lane functions (ADR
-- 045, docs/decisions/agents/045-faas-on-fc-invoke-sandbox-runtime.md; R1
-- execution split, docs/plans/2026-07-14-embervm-r1-zip-lane-spec-and-plan.md
-- Task 9). One row per function name; global name uniqueness is the PK
-- (visibility is a flag, not a namespace).
--
-- last_smoke_at doubles as the visibility gate: NULL means registered but not
-- yet smoke-tested (not visible), a set value means the last registered zip
-- passed its test-run gate (Task 10) and is servable (Task 11 filters on it).
-- No separate boolean column; re-registering a name clears last_smoke_at back
-- to NULL until the new zip smokes again.
--
-- Private-tier schema: the private `app` role needs no explicit GRANT here
-- (mirrors 20260712000000_home_cluster_snapshot.sql). public_reader grants
-- land in a later PR when faas goes public.

CREATE SCHEMA IF NOT EXISTS faas;

CREATE TABLE faas.function (
    name          TEXT PRIMARY KEY,
    visibility    TEXT NOT NULL CHECK (visibility IN ('private','public')),
    runtime       TEXT NOT NULL,
    handler       TEXT NOT NULL,
    zip_sha256    TEXT NOT NULL,
    code_uri      TEXT NOT NULL,
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_smoke_at TIMESTAMPTZ
);
