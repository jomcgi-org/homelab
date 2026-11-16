-- Idempotent status reads share a stable request fingerprint across retries
-- and writer takeovers. Keep counts and epochs, never bodies or credentials.
CREATE TABLE status_read (
    message_id text PRIMARY KEY,
    writer_epoch bigint NOT NULL,
    attempts bigint NOT NULL DEFAULT 1,
    attempted_at timestamptz NOT NULL DEFAULT now()
);
