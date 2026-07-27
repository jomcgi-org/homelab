-- Ensure only one monolith replica executes a queued agent-session message.

ALTER TABLE agent_sessions.pending_messages
    ADD COLUMN claimed_by_replica TEXT;
