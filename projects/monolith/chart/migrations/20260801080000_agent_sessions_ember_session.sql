ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN ember_session_id TEXT,
    ADD COLUMN ember_session_token TEXT,
    ADD COLUMN ember_session_expires_at BIGINT;

-- Pre-fix rows may carry a CLI id from an invoke that 404ed, and the
-- corresponding VM/transcript is gone. A CLI id without its ember binding
-- asks a fresh VM to --resume a transcript it never had (503). Clear it.
UPDATE agent_sessions.agent_sessions SET cli_session_id = NULL;
