ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN recovery_workspace_loss BOOLEAN;

ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN recovery_completed_at TIMESTAMPTZ;
