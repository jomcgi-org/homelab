-- Record the discord id of the reply Bosun sends for an ambient engage, so the
-- /improve-ambient loop can join an engagement to its reply (and thus to
-- reaction_event rows) exactly, instead of guessing by a time window. Nullable:
-- backfills forward from this migration, and stays null for the agent-thread
-- path (which has no single in-channel reply).
ALTER TABLE chat.attention_decision
    ADD COLUMN IF NOT EXISTS reply_message_id TEXT;
CREATE INDEX IF NOT EXISTS attention_decision_reply_message_idx
    ON chat.attention_decision (reply_message_id);
