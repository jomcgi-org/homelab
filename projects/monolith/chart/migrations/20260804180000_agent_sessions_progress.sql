ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN progress_token TEXT NULL;

ALTER TABLE agent_sessions.pending_messages
    ADD COLUMN partial_text TEXT NULL;
