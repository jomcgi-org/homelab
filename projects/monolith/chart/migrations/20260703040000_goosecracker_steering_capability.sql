-- Bind the guest steering fetch to an unguessable per-session capability token
-- instead of the Discord thread snowflake (ADR 035 Phase 2 hardening).
--
-- The steering endpoint was keyed on thread_id, which is a guessable Discord
-- snowflake: a compromised guest that learned another thread's id could read,
-- deny, or pollute that victim thread's steering. The runner now injects a
-- token-keyed URL instead, so a guest can only ever address its own thread.
--
-- Safe with existing data: rows default to '' and get a token lazily on next
-- dispatch (ensure_steering_token assigns one on first use, same pattern as
-- artifact_id). The partial unique index excludes the empty default so
-- multiple un-migrated rows never collide on ''.
ALTER TABLE chat.goosecracker_sessions ADD COLUMN IF NOT EXISTS steering_token TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS goosecracker_sessions_steering_token_key
    ON chat.goosecracker_sessions (steering_token) WHERE steering_token <> '';
