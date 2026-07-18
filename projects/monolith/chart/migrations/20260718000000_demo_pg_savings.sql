-- demo_pg_savings: the all-time "memory saved while asleep" counter for the
-- demo-postgres exhibit (embervm R4 stateful demo). Every visitor's status
-- poll accrues into this single row so the headline reflects every visitor,
-- not just the current session.
--
-- Accumulation is lazy on the status endpoint with a generation-validated
-- credit rule: the demo VM can only wake when something connects, so if two
-- consecutive samples are both state == 'banked' with the SAME generation, it
-- provably slept the whole gap between samples and that whole gap is credited
-- at 512 MiB per second. Any other transition (a different generation, or
-- either sample not banked) credits nothing, which makes the counter a
-- conservative undercount rather than a guess.
--
-- Single-row table (id fixed to 1 by the CHECK constraint); last_sample_at /
-- last_state / last_generation are the previous sample this credit rule
-- compares against, not an audit log.
--
-- Private-tier schema: the private `app` role needs no explicit GRANT here
-- (mirrors 20260712000000_home_cluster_snapshot.sql and
-- 20260714000000_faas_function.sql).

CREATE TABLE demo_pg_savings (
    id                smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    total_mib_seconds double precision NOT NULL DEFAULT 0,
    last_sample_at    timestamptz,
    last_state        text,
    last_generation   bigint
);
