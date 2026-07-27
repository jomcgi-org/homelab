-- Ensure only one monolith replica executes a queued agent-session message,
-- with lease expiry for recovery from crashed replicas.

ALTER TABLE agent_sessions.pending_messages
    ADD COLUMN claimed_by_replica TEXT,
    ADD COLUMN claimed_at TIMESTAMPTZ;
