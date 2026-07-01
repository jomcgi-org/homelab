-- Make goosecracker Discord thread sessions conversational.
--
-- Adds five columns to chat.goosecracker_sessions:
--   recipe  - "artifact" or "agent", so the reply handler knows which code path to use
--   tier    - model/guest tier (empty = in-cluster Qwen, "artifact" = OpenRouter)
--   repo    - repository name for agent sessions (homelab, loom, etc.)
--   running - true while a turn is in flight, prevents concurrent dispatches
--   pending - newline-joined replies queued while running=true; drained on turn completion
--
-- Safe to run with data: all columns have defaults that match the pre-migration
-- behaviour (existing rows are artifact sessions with no in-flight turn).

ALTER TABLE chat.goosecracker_sessions
    ADD COLUMN IF NOT EXISTS recipe  TEXT NOT NULL DEFAULT 'artifact',
    ADD COLUMN IF NOT EXISTS tier    TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS repo    TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS running BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS pending TEXT NOT NULL DEFAULT '';
