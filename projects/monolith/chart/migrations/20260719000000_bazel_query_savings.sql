-- bazel_query_savings: the all-time "estimated cold analysis time skipped"
-- counter for the bazel skyframe query demo (ADR embervm/010).
--
-- Unlike demo_pg_savings (a polled banked-to-banked delta), this counter has
-- no state machine: every successful POST /query already knows exactly how
-- much cold-analysis time that run skipped (the recorded cold baseline of
-- 13.8s minus that run's measured wall_ms), so accrual is a direct add,
-- once per successful query, inside ember_public.bazel_core.run_query's
-- caller.
--
-- Single-row table (id fixed to 1 by the CHECK constraint), same shape as
-- demo_pg_savings.
--
-- Private-tier schema: the private `app` role needs no explicit GRANT here
-- (mirrors 20260718000000_demo_pg_savings.sql).

CREATE TABLE bazel_query_savings (
    id                      smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    total_analysis_s_saved  double precision NOT NULL DEFAULT 0
);
