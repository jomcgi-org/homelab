-- Configurable Discord message triggers (services/002 phase 3, issue #3903).
-- Empty channel_ids or user_ids arrays mean all. last_fired_at is advanced by
-- a conditional UPDATE before dispatch, enforcing cooldown across replicas.
CREATE TABLE IF NOT EXISTS chat.triggers (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    pattern TEXT NOT NULL,
    channel_ids JSONB NOT NULL DEFAULT '[]'::JSONB,
    user_ids JSONB NOT NULL DEFAULT '[]'::JSONB,
    action_type TEXT NOT NULL,
    action_config JSONB NOT NULL,
    cooldown_secs INTEGER NOT NULL DEFAULT 0,
    last_fired_at TIMESTAMPTZ,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_by_user_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT triggers_name_length CHECK (length(name) BETWEEN 1 AND 100),
    CONSTRAINT triggers_pattern_length CHECK (length(pattern) BETWEEN 1 AND 512),
    CONSTRAINT triggers_channel_ids_array CHECK (jsonb_typeof(channel_ids) = 'array'),
    CONSTRAINT triggers_user_ids_array CHECK (jsonb_typeof(user_ids) = 'array'),
    CONSTRAINT triggers_action_config_object CHECK (jsonb_typeof(action_config) = 'object'),
    CONSTRAINT triggers_action_type_valid CHECK (
        action_type IN ('respond', 'crosspost', 'agent_run')
    ),
    CONSTRAINT triggers_cooldown_valid CHECK (
        cooldown_secs >= 0 AND cooldown_secs <= 604800
    )
);

CREATE INDEX IF NOT EXISTS triggers_enabled_id
    ON chat.triggers (id) WHERE enabled;
