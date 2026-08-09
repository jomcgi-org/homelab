-- platform_probe is a last-result-wins latch, not an audit log: one row per
-- probe name records the latest observation and its recovery time. Same shape
-- as ember_synthetic_probe, but domain-neutral, because the first consumer is
-- CD health and putting that in a table named "ember_*" would mislead whoever
-- greps it during an incident.
--
-- Why a latch exists at all: the tier that CAN compute platform health (private,
-- which has ArgoCD reads and a GitHub token) is not the tier that is externally
-- reachable (public, which is what UptimeRobot polls). Two processes, so the
-- value has to be handed off, and Postgres is the handoff.
--
-- The writer is the private monolith running as the app role, so no explicit
-- GRANT is needed here. The reader is the public tier; its grant is a separate
-- migration, mirroring the ember pair.

CREATE TABLE platform_probe (
    name TEXT PRIMARY KEY,
    ok BOOLEAN NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    checked_at TIMESTAMPTZ NOT NULL,
    last_ok_at TIMESTAMPTZ
);
