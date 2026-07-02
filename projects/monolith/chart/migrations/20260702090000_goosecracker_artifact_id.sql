-- Unguessable capability id for published artifacts (ADR 024 amendment).
--
-- Artifacts were published under the Discord thread id, an enumerable snowflake,
-- so the live URL was publicly guessable. Add a per-thread random artifact_id
-- (assigned on first publish, reused on re-publish for hot-reload) so the URL is
-- an unguessable capability that is still safe to share by link.
--
-- Safe to run with data: existing rows default to '' and get a random id on
-- their next publish.
ALTER TABLE chat.goosecracker_sessions
    ADD COLUMN IF NOT EXISTS artifact_id TEXT NOT NULL DEFAULT '';
