-- chat.goosecracker_sessions: the per-Discord-thread curated transcript for the
-- goosecracker artifact agent (ADR 024 Task 4).
--
-- /goosecracker opens a Discord thread and runs the artifact agent. Each
-- follow-up message from the owner in that thread re-runs goose from scratch
-- (Model B) with the FULL accumulated transcript, so the rebuilt artifact
-- reflects every instruction so far rather than just the latest one. The
-- transcript is the owner's instructions only (never ambient chatter or the
-- bot's own replies), keyed by the Discord thread id, which is also the stable
-- ARTIFACT_ID so every re-run hot-reloads the same artifact.
--
-- One row per thread; the transcript column accumulates the owner turns.

CREATE TABLE chat.goosecracker_sessions (
    discord_thread  TEXT PRIMARY KEY,
    transcript      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
