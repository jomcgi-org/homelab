ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN discord_thread TEXT;

CREATE UNIQUE INDEX agent_sessions_discord_thread_key
    ON agent_sessions.agent_sessions (discord_thread);
