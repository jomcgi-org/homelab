-- chat_public.shared_snapshots: opt-in, read-only, immutable shares of a public
-- chat transcript (ADR 005 follow-up, "share this chat").
--
-- A snapshot is minted SERVER-SIDE from the stored, server-authoritative
-- transcript, never from client-supplied message content: a forged request body
-- must not be able to put words in the model's mouth in a publicly-shareable
-- artifact. Once created a snapshot is immutable and read-only (no UPDATE path
-- in the application). The id is an opaque CSPRNG token (secrets.token_urlsafe),
-- same posture as a session id, so a snapshot url is unguessable.
--
-- source_session_id is kept for forensics only and is intentionally NOT a
-- cascading FK: a shared snapshot must OUTLIVE its session. When a session row
-- is purged (TTL or takedown) the snapshot survives with source_session_id set
-- to NULL (ON DELETE SET NULL), so retiring a session never silently deletes a
-- link a visitor may have shared. Takedown of a snapshot itself is a separate
-- DELETE on this table.

CREATE TABLE chat_public.shared_snapshots (
    id                TEXT PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    transcript        JSONB NOT NULL,
    message_count     INTEGER NOT NULL DEFAULT 0,
    source_session_id TEXT
        REFERENCES chat_public.sessions(id) ON DELETE SET NULL,
    CONSTRAINT shared_snapshots_message_count_nonneg_chk CHECK (message_count >= 0)
);

CREATE INDEX shared_snapshots_created ON chat_public.shared_snapshots (created_at);

-- Grants mirror the existing chat_public tables. public_writer mints snapshots
-- (INSERT) and reads them back on create (SELECT); public_reader reads them for
-- the public read route (SELECT). Both already auto-receive these via the ALTER
-- DEFAULT PRIVILEGES set in 20260617030000_chat_public.sql; the explicit grants
-- below are belt-and-braces so the permission is obvious at the table that needs
-- it. (public_reader's default privilege grant lives in the public_reader role
-- migration; the explicit GRANT here covers the read path regardless.)
GRANT SELECT, INSERT ON chat_public.shared_snapshots TO public_writer;
GRANT SELECT ON chat_public.shared_snapshots TO public_reader;
