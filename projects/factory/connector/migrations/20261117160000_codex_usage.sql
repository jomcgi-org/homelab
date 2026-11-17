-- The broker records calls before contacting the provider. The connector
-- links a new session's early calls when its dispatch is acknowledged.
CREATE TABLE codex_call (
    id bigserial PRIMARY KEY,
    atespace text NOT NULL,
    session_id text NOT NULL,
    actor_uid text NOT NULL,
    delivery_attempt_id bigint REFERENCES delivery_attempt (id),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX codex_call_unassigned_idx ON codex_call (atespace, session_id)
    WHERE delivery_attempt_id IS NULL;
CREATE INDEX codex_call_attempt_idx ON codex_call (delivery_attempt_id);
CREATE INDEX delivery_attempt_session_idx
    ON delivery_attempt (kagent_namespace, lower(context_id), id DESC);

-- A repeated completion (including a replayed response) is counted once.
-- Missing usage stays absent: it must not look like a measured zero.
CREATE TABLE codex_usage (
    call_id bigint PRIMARY KEY REFERENCES codex_call (id),
    response_id text NOT NULL UNIQUE,
    input_tokens bigint NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens bigint NOT NULL CHECK (cached_input_tokens >= 0 AND cached_input_tokens <= input_tokens),
    output_tokens bigint NOT NULL CHECK (output_tokens >= 0)
);

ALTER TABLE run
    ADD COLUMN input_tokens bigint,
    ADD COLUMN cached_input_tokens bigint,
    ADD COLUMN output_tokens bigint;
