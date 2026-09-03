ALTER TABLE agent_sessions.agent_sessions ADD COLUMN kg_extracted_turn_seq INTEGER;

CREATE INDEX agent_sessions_kg_pending_idx
    ON agent_sessions.agent_sessions (last_turn_at)
    WHERE status IN ('completed','warn');
