CREATE TABLE IF NOT EXISTS agent_sessions.probe_observations (
    permit_id BIGINT PRIMARY KEY,
    identity_sha256 TEXT,
    guest_id TEXT,
    generation BIGINT,
    invoke_started_at BIGINT,
    cp_updated_at BIGINT,
    reason TEXT NOT NULL,
    evidence_json TEXT,
    checked_at TIMESTAMPTZ NOT NULL,
    settled_at TIMESTAMPTZ
);
