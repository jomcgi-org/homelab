-- Store the Claude CLI's session ID for resumption across turns.

ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN cli_session_id TEXT;
