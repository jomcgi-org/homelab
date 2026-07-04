-- Directive-proposal outbox rows (ADR 035 Phase 4 observer). The observer job
-- enqueues a normal content post tagged kind='directive_proposal' whose
-- payload_json carries the channel_id / directive_change / evidence /
-- motivating_message_id that the leader's drain post-hook needs to wire the
-- propose-then-confirm flow against the message id it just posted. Plain posts
-- and reaction/edit rows leave kind='' and payload_json NULL, so this is purely
-- additive: existing rows and producers are unaffected. Columns mirror the
-- SQLModel fields so the SQLite test fixtures build the same shape.
ALTER TABLE chat.discord_outbox
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS payload_json TEXT;
