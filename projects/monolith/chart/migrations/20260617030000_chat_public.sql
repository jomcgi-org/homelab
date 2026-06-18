-- chat_public schema: anonymous public chat sessions and transcripts (ADR 005).
--
-- This is the first PUBLIC-tier write path. Sessions, the pseudonymous
-- user/session details, and the transcript live here and are written by a
-- dedicated `chat_public` role on the Postgres primary, distinct from the
-- read-only `public_reader` role (which stays the note-retrieval path and is
-- never a writer). The role gets DML on this one schema and nothing else, so a
-- compromise of the public chat path can write its own sessions and read public
-- notes, and nothing more.
--
-- No raw IP or PII is stored: only hashes (ip_hash, user_agent_hash), a coarse
-- country code (from the Cloudflare CF-IPCountry header), and the Turnstile
-- outcome. Purge tooling (TTL job + on-demand takedown) lands in a later phase.

CREATE SCHEMA IF NOT EXISTS chat_public;

-- One row per anonymous session. The session row, not the cookie, is the
-- authority for every budget (turn_count, total_tokens). The cookie is an
-- opaque id over this row.
CREATE TABLE chat_public.sessions (
    id               TEXT PRIMARY KEY,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    turn_count       INTEGER NOT NULL DEFAULT 0,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    ip_hash          TEXT,
    turnstile_outcome TEXT,
    country          TEXT,
    user_agent_hash  TEXT,
    status           TEXT NOT NULL DEFAULT 'active',
    rolling_summary  TEXT,
    CONSTRAINT sessions_status_chk
        CHECK (status IN ('active', 'expired', 'purged')),
    CONSTRAINT sessions_turn_count_nonneg_chk CHECK (turn_count >= 0),
    CONSTRAINT sessions_total_tokens_nonneg_chk CHECK (total_tokens >= 0)
);

CREATE INDEX sessions_last_seen ON chat_public.sessions (last_seen_at);

-- The transcript. One row per message, ordered within a session by created_at.
-- Server-authoritative: the browser never sends history, only the new user
-- message, so this table is the sole record of the conversation.
CREATE TABLE chat_public.messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES chat_public.sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tokens      INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT messages_role_chk CHECK (role IN ('user', 'assistant', 'system')),
    CONSTRAINT messages_tokens_nonneg_chk CHECK (tokens >= 0)
);

CREATE INDEX messages_session_time ON chat_public.messages (session_id, created_at);

-- Role creation: in production the role is created by CNPG (spec.managed.roles,
-- which runs as a superuser), because this Atlas migration runs as the `app`
-- role, which owns the schema but lacks CREATEROLE. The DO block below is a
-- no-op in production (the role already exists); it is here so the CNPG-less
-- test Postgres (which applies every migration as a superuser) creates the role
-- and can exercise these GRANTs. The GRANTs run as `app`, which owns every
-- object granted.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'chat_public') THEN
        CREATE ROLE chat_public NOLOGIN;
    END IF;
END $$;

-- DML scoped strictly to the chat_public schema. ALTER DEFAULT PRIVILEGES
-- covers tables a future migration adds to this schema, so the grant does not
-- silently rot. The role is never granted any other schema (knowledge, ships,
-- hikes, stars, home, etc.).
GRANT USAGE ON SCHEMA chat_public TO chat_public;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA chat_public TO chat_public;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA chat_public TO chat_public;
ALTER DEFAULT PRIVILEGES IN SCHEMA chat_public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO chat_public;
ALTER DEFAULT PRIVILEGES IN SCHEMA chat_public
    GRANT USAGE, SELECT ON SEQUENCES TO chat_public;
