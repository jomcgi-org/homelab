ALTER TABLE agent_sessions.pending_messages
    ADD COLUMN dispatch_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN last_dispatch_at TIMESTAMPTZ;
