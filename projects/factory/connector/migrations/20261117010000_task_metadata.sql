-- Oversized transcripts are a visible failed outcome, without an engineering retry.
ALTER TABLE delivery_attempt DROP CONSTRAINT delivery_attempt_error_class_check;
ALTER TABLE delivery_attempt ADD CONSTRAINT delivery_attempt_error_class_check
    CHECK (error_class IN ('engineering', 'credential', 'quota', 'canceled', 'abandoned', 'bounded_output'));

-- Poll attempts commit under the writer fence before each metadata request.
-- The ID survives transport errors and restarts, while each poll reads fresh state.
CREATE TABLE task_metadata_read (
    message_id text PRIMARY KEY,
    attempt_id bigint NOT NULL REFERENCES delivery_attempt (id),
    task_id text NOT NULL,
    context_id text NOT NULL,
    writer_epoch bigint NOT NULL,
    attempts integer NOT NULL DEFAULT 1,
    attempted_at timestamptz NOT NULL DEFAULT now()
);
