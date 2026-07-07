-- Log of chat-agent replies that leaked tool-call scaffolding (chat.reply_sanitize).
-- One row per reply where the small model dumped <tool_call>/<arg_*> scaffolding
-- into its answer and the shield had to scrub and, when needed, run the bounded
-- model-repair loop. Kept so the copy can be evaluated later and the reply/plan
-- prompts iterated against real failures. Safe to create empty.
CREATE TABLE IF NOT EXISTS chat.agent_reply_repair (
    id BIGSERIAL PRIMARY KEY,
    channel_id TEXT NOT NULL DEFAULT '',
    author_id TEXT NOT NULL DEFAULT '',
    route TEXT NOT NULL DEFAULT 'chat',
    markers TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    scrubbed_text TEXT NOT NULL DEFAULT '',
    final_text TEXT NOT NULL DEFAULT '',
    repair_attempts INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT 'clean_after_repair',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agent_reply_repair_outcome_valid
        CHECK (outcome IN ('clean_after_repair', 'still_dirty'))
);
CREATE INDEX IF NOT EXISTS agent_reply_repair_created ON chat.agent_reply_repair (created_at DESC);
