-- Reaction lifecycle + self-heal for goosecracker agent threads.
--
-- goosecracker_sessions gains per-message queue tracking and in-flight turn
-- bookkeeping so the runner can drive a ⏳→👀→✅/❌ reaction lifecycle on the
-- user's own message (replacing the noisy "Queued" text replies) and so an
-- orphaned turn (leader restart / stale wedge) can be reclaimed losslessly:
--   pending_message_ids - newline-joined Discord message ids, parallel to pending
--   inflight_task        - the running turn's task text, kept for lossless reclaim
--   inflight_ack_ids     - message ids the running turn will resolve with reactions
--   running_since        - when the turn went running (stale-timeout backstop)
--   runner_instance      - boot token of the owning process (reset detector)
--
-- discord_outbox gains a reaction verb (target_message_id/reaction/reaction_remove)
-- so the off-loop runner drives reactions through the same leader-safe drain.
--
-- All columns have defaults matching pre-migration behaviour, so existing rows
-- are unaffected (idle artifact/agent sessions with no in-flight turn).

ALTER TABLE chat.goosecracker_sessions
    ADD COLUMN IF NOT EXISTS pending_message_ids TEXT        NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS inflight_task        TEXT        NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS inflight_ack_ids     TEXT        NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS running_since        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS runner_instance      TEXT        NOT NULL DEFAULT '';

ALTER TABLE chat.discord_outbox
    ADD COLUMN IF NOT EXISTS target_message_id TEXT,
    ADD COLUMN IF NOT EXISTS reaction          TEXT,
    ADD COLUMN IF NOT EXISTS reaction_remove   BOOLEAN NOT NULL DEFAULT false;
