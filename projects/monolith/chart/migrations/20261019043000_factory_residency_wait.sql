ALTER TABLE agent_sessions.pending_messages
    ADD COLUMN capacity_started_at TIMESTAMPTZ,
    ADD COLUMN capacity_state TEXT,
    ADD COLUMN capacity_intent_json TEXT,
    ADD COLUMN capacity_next_poll_at TIMESTAMPTZ;
