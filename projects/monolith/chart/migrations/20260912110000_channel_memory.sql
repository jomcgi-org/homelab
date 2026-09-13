-- Per-channel durable notes and rolling-summary prompt configuration.

CREATE TABLE chat.channel_memory (
    channel_id TEXT PRIMARY KEY,
    summary_prompt_user TEXT,
    summary_prompt_channel TEXT,
    summary_style TEXT,
    notes TEXT,
    updated_by_user_id TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT channel_memory_channel_id_length CHECK (length(channel_id) BETWEEN 1 AND 64),
    CONSTRAINT channel_memory_user_prompt_length CHECK (length(summary_prompt_user) <= 8000),
    CONSTRAINT channel_memory_channel_prompt_length CHECK (length(summary_prompt_channel) <= 8000),
    CONSTRAINT channel_memory_summary_style_length CHECK (length(summary_style) <= 500),
    CONSTRAINT channel_memory_notes_length CHECK (length(notes) <= 12000),
    CONSTRAINT channel_memory_updated_by_length CHECK (length(updated_by_user_id) <= 64)
);
