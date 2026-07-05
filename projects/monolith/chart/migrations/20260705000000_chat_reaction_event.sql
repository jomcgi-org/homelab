-- Persist human reactions on Bosun's own messages, so the /improve-ambient
-- feedback loop has a ground-truth fluidity/productivity signal (a thumbs up
-- vs down on a reply) instead of only inferring from follow-up text. Only
-- reactions whose reacted message was authored by the bot are stored
-- (target_is_bot is always true here); the column is explicit to leave room to
-- widen capture later. reactor_id is the human who reacted (never the bot's own
-- seed reactions). action distinguishes add/remove so a removal cancels an
-- earlier signal.
CREATE TABLE IF NOT EXISTS chat.reaction_event (
    id            BIGSERIAL PRIMARY KEY,
    channel_id    TEXT NOT NULL DEFAULT '',
    message_id    TEXT NOT NULL DEFAULT '',
    target_is_bot BOOLEAN NOT NULL DEFAULT TRUE,
    emoji         TEXT NOT NULL DEFAULT '',
    reactor_id    TEXT NOT NULL DEFAULT '',
    action        TEXT NOT NULL DEFAULT 'add',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT reaction_event_action_valid CHECK (action IN ('add', 'remove'))
);
CREATE INDEX IF NOT EXISTS reaction_event_message_idx ON chat.reaction_event (message_id);
CREATE INDEX IF NOT EXISTS reaction_event_created_idx ON chat.reaction_event (created_at);
