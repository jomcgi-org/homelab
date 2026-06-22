-- chat.discord_outbox: a leader-safe outbox for Discord posts.
--
-- The Discord bot is a leader-only singleton (it runs on exactly one replica,
-- gated by leader election). Posting a message therefore can't be done by an
-- arbitrary replica or an ephemeral Argo job - those have no bot connection.
-- Instead, any producer inserts a row here, and the leader's bot drains the
-- table and posts. This is the same "compute writes a row, a reader consumes
-- it" pattern as the observability/calendar snapshots, applied to posting, and
-- it makes the notify path leader-safe as the web tier scales horizontally.
--
-- content is plain text; embed_json is a JSON-serialised Discord embed (stored
-- as TEXT, not JSONB, so the SQLModel-metadata SQLite test fixtures build it
-- without a JSONB type). Exactly one of content/embed_json is set per row.

CREATE TABLE chat.discord_outbox (
    id          BIGSERIAL PRIMARY KEY,
    channel_id  TEXT NOT NULL,
    content     TEXT,
    embed_json  TEXT,
    level       TEXT NOT NULL DEFAULT 'info',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_at   TIMESTAMPTZ,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    CONSTRAINT discord_outbox_content_or_embed
        CHECK (content IS NOT NULL OR embed_json IS NOT NULL)
);

-- The drain scans only unposted rows oldest-first; a partial index keeps that
-- cheap as posted rows accumulate.
CREATE INDEX discord_outbox_pending
    ON chat.discord_outbox (created_at)
    WHERE posted_at IS NULL;
