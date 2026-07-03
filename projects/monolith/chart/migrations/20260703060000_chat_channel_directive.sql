-- Living per-channel behavioural directives (ADR 035 phase 5): versioned, one
-- active row per channel, full history kept. A proposed (not yet confirmed) row
-- is active=false; the 10-min propose-then-confirm flow flips it active.
CREATE TABLE IF NOT EXISTS chat.channel_directive (
    id BIGSERIAL PRIMARY KEY,
    channel_id TEXT NOT NULL,
    directive TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    seed_ref TEXT NOT NULL DEFAULT '',
    updated_by_user_id TEXT NOT NULL DEFAULT '',
    motivating_message_id TEXT NOT NULL DEFAULT '',
    proposal_message_id TEXT NOT NULL DEFAULT '',
    previous_version INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Exactly one active directive per channel.
CREATE UNIQUE INDEX IF NOT EXISTS channel_directive_active_key
    ON chat.channel_directive (channel_id) WHERE active;
-- Look up a pending proposal by the bot's proposal message id.
CREATE INDEX IF NOT EXISTS channel_directive_proposal
    ON chat.channel_directive (proposal_message_id) WHERE proposal_message_id <> '';

-- Per-user style preferences (ADR 035 phase 5): layered on top of the channel
-- directive at reply time, not merged into it. One active pref per user.
CREATE TABLE IF NOT EXISTS chat.user_style_pref (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    pref TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by_user_id TEXT NOT NULL DEFAULT '',
    motivating_message_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS user_style_pref_active_key
    ON chat.user_style_pref (user_id) WHERE active;
