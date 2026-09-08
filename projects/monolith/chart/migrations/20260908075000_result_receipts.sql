CREATE TABLE agent_sessions.result_receipts (
    id text PRIMARY KEY,
    token_sha256 text NOT NULL UNIQUE,
    session_id bigint NOT NULL,
    local_session_id text NOT NULL,
    seq integer NOT NULL CHECK (seq > 0),
    dispatch_count integer NOT NULL CHECK (dispatch_count > 0),
    claim_owner text NOT NULL,
    guest_id text NOT NULL,
    request_sha256 text NOT NULL,
    created_at timestamptz NOT NULL,
    accept_until timestamptz NOT NULL,
    retain_until timestamptz NOT NULL,
    superseded_at timestamptz,
    received_at timestamptz,
    result_sha256 text,
    result_body bytea
);
CREATE INDEX result_receipts_session_seq_idx
    ON agent_sessions.result_receipts (session_id, seq);
CREATE INDEX ix_agent_sessions_result_receipts_retain_until
    ON agent_sessions.result_receipts (retain_until);
