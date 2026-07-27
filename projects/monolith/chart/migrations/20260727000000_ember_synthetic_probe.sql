-- ember_synthetic_probe is a last-result-wins latch, not an audit log: one
-- row per demo records the latest synthetic observation and its recovery time.
-- The writer is the private jobs pod, running as the app role, so no explicit
-- GRANT is needed here. The reader is the public tier.

CREATE TABLE ember_synthetic_probe (
    demo TEXT PRIMARY KEY,
    ok BOOLEAN NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    latency_ms DOUBLE PRECISION,
    checked_at TIMESTAMPTZ NOT NULL,
    last_ok_at TIMESTAMPTZ
);
