-- Notification attempts contain only a hashed daily key and fixed RPC phase.
-- Writer.Tx checks the epoch and commits this record before each HTTP request.
CREATE TABLE notify_attempt (
    dedupe_key text NOT NULL,
    phase text NOT NULL CHECK (phase IN ('token', 'initialize', 'notifications/initialized', 'tools/call')),
    writer_epoch bigint NOT NULL,
    attempts bigint NOT NULL DEFAULT 1,
    last_attempt_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dedupe_key, phase)
);
