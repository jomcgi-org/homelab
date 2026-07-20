-- demo_sg_savings: the all-time "scan time saved versus a hosted
-- single-file scan" counter for the /ember/semgrep exhibit.
--
-- Unlike demo_pg_savings (a polled banked-to-banked delta), this counter has
-- no state machine: every successful scan already knows exactly how much
-- time it saved (semgrep_core.saved_ms(scan_ms), the HOSTED_SCAN_MEDIAN_MS
-- baseline minus that scan's measured wall time), so accrual is a direct
-- add, once per successful POST /scan, inside
-- ember_public.semgrep_core.record_demo_sg_savings_core. Mirrors
-- bazel_query_savings' event-triggered accrual shape.
--
-- Single-row table (id fixed to 1 by the CHECK constraint): scans is the
-- all-time scan count, actual_ms is the summed measured scan wall time,
-- saved_ms is the summed credit versus the hosted baseline.
--
-- Private-tier schema: the private `app` role needs no explicit GRANT here
-- (mirrors 20260718000000_demo_pg_savings.sql and
-- 20260719000000_bazel_query_savings.sql).

CREATE TABLE demo_sg_savings (
    id        smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    scans     bigint NOT NULL DEFAULT 0,
    actual_ms bigint NOT NULL DEFAULT 0,
    saved_ms  bigint NOT NULL DEFAULT 0
);
