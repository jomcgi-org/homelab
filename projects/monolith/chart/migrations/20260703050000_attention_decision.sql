-- Attention-gate decision log (ADR 035 phase 3). One row per attention-gate
-- decision on a message in an ambient-mode channel: every "engage" is logged,
-- "ignore" rows are sampled at the app layer to bound volume. Used to tune the
-- classifier and to audit what the bot chose to engage with. Safe to create
-- empty; directive_version stays 0 until phase 5 wires channel directives.
CREATE TABLE IF NOT EXISTS chat.attention_decision (
    id BIGSERIAL PRIMARY KEY,
    channel_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT 'ignore',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    directive_version INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT attention_decision_decision_valid CHECK (decision IN ('engage', 'ignore'))
);
CREATE INDEX IF NOT EXISTS attention_decision_channel_created ON chat.attention_decision (channel_id, created_at DESC);
