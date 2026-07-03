-- Live progress message id for goosecracker agent/artifact threads.
--
-- The bot posts one live message per run and edits it in place with the stage
-- checklist. On completion the runner now overwrites that SAME message with the
-- final result via a durable outbox edit (leader-safe), instead of posting a
-- separate second message. Store the message id on the session row so the runner
-- (off-loop, possibly another replica) can address it durably, and so a
-- cross-process reclaim reloads it from the row rather than losing it.
--
-- Safe to run with data: existing rows default to '' (no live message, so the
-- runner falls back to posting the result as a new message).
ALTER TABLE chat.goosecracker_sessions
    ADD COLUMN IF NOT EXISTS progress_message_id TEXT NOT NULL DEFAULT '';
