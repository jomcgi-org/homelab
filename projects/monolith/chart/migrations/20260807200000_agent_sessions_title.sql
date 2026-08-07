-- Qwen-generated session display name; title_turn_seq records the turn
-- the name was generated from so the leader loop can refresh it when
-- newer turns land.
ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN title TEXT,
    ADD COLUMN title_turn_seq INTEGER;
