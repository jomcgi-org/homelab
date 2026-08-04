ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN prior_ember_lineage_id TEXT,
    ADD COLUMN prior_cli_session_id TEXT;
