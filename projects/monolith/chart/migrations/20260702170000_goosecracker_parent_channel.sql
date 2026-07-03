-- Discord parent channel id for agent (goosecracker) threads.
--
-- The /agent command opens a NEW thread and dispatches goose against it, but the
-- thread has no history: the conversational context (recent messages, per-user
-- and per-channel rolling summaries) lives on the PARENT channel the command was
-- run from. Store that parent channel id on the session row so the runner can
-- fetch channel-scoped context for a conversational reply, and so a cross-process
-- reclaim (leader restart) reloads it from the row rather than losing it with the
-- original dispatch params.
--
-- Safe to run with data: existing rows default to '' (an empty parent means the
-- runner falls back to the deterministic summary, no channel context).
ALTER TABLE chat.goosecracker_sessions
    ADD COLUMN IF NOT EXISTS parent_channel_id TEXT NOT NULL DEFAULT '';
