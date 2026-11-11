-- Server-owned restore context is persisted atomically with the first turn.
ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN IF NOT EXISTS workspace_handoff_json TEXT;

ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN IF NOT EXISTS capture_evidence_ttl_seconds INTEGER;
