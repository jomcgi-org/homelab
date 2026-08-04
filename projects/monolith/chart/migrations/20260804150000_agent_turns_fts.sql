-- Full-text search on agent turns (prompt + result_text).
-- Generated stored column so it auto-updates on insert/update.

ALTER TABLE agent_sessions.agent_turns
    ADD COLUMN fts_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', prompt || ' ' || result_text)) STORED;

CREATE INDEX agent_turns_fts_idx ON agent_sessions.agent_turns USING gin (fts_vector);
