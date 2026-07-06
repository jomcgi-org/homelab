-- grimoire_chat schema: anonymous public chat over the Grimoire sourcebook corpus
-- (ADR security/005 posture, mirrored from chat_public).
--
-- This is a second PUBLIC-tier write path, structurally identical to
-- chat_public (20260617030000_chat_public.sql and its follow-ups) but for the
-- Grimoire (D&D sourcebook) corpus instead of Joe's notes. Sessions, the
-- pseudonymous user/session details, the transcript, the response cache, and the
-- shared snapshots live here and are written by the SAME dedicated
-- `public_writer` role on the Postgres primary, distinct from the read-only
-- `public_reader` role (which stays the corpus-retrieval path and is never a
-- writer). public_writer is the generic public-tier write identity, so it simply
-- gains DML on this second schema and nothing else: a compromise of the grimoire
-- chat path can write its own sessions and read the public Grimoire corpus, and
-- nothing more.
--
-- No raw IP or PII is stored: only hashes (ip_hash, user_agent_hash), a coarse
-- country code (from the Cloudflare CF-IPCountry header), and the Turnstile
-- outcome. This consolidates into one migration what chat_public built across
-- four (schema+sessions+messages, messages.touched, response_cache,
-- shared_snapshots) plus the public_reader schema-USAGE grant for shared reads.

CREATE SCHEMA IF NOT EXISTS grimoire_chat;

-- One row per anonymous session. The session row, not the cookie, is the
-- authority for every budget (turn_count, total_tokens). The cookie is an
-- opaque id over this row.
CREATE TABLE grimoire_chat.sessions (
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

CREATE INDEX sessions_last_seen ON grimoire_chat.sessions (last_seen_at);

-- The transcript. One row per message, ordered within a session by created_at.
-- Server-authoritative: the browser never sends history, only the new user
-- message, so this table is the sole record of the conversation. `touched` is
-- each assistant turn's grounding (the corpus passages/entities it drew from) as
-- a [{id, title}, ...] JSONB array, empty for user turns, so a shared snapshot
-- can render the same GROUNDED IN chips the live app shows.
CREATE TABLE grimoire_chat.messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES grimoire_chat.sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tokens      INTEGER NOT NULL DEFAULT 0,
    touched     JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT messages_role_chk CHECK (role IN ('user', 'assistant', 'system')),
    CONSTRAINT messages_tokens_nonneg_chk CHECK (tokens >= 0)
);

CREATE INDEX messages_session_time ON grimoire_chat.messages (session_id, created_at);

-- Durable, cross-pod cache of grimoire-chat answers. A simple key/value table:
-- cache_key is a hash of (normalized_message, prompt_version, corpus_watermark),
-- so an identical question hits regardless of trivial whitespace/case, and any
-- change to the system prompt, the model, or the published Grimoire corpus
-- invalidates the entry because the key changes. The component parts are stored
-- alongside the key for debuggability; touched is the node_touched grounding list
-- replayed on a hit. (The column keeps the chat_public name `notes_watermark` so
-- the model/migration shape stays in lockstep; for grimoire_chat it holds the
-- Grimoire-corpus watermark, see grimoire_chat/cache.py.)
CREATE TABLE grimoire_chat.response_cache (
    cache_key          TEXT PRIMARY KEY,
    normalized_message TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,
    notes_watermark    TEXT NOT NULL,
    response_text      TEXT NOT NULL,
    touched            JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    hit_count          INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT response_cache_hit_count_nonneg_chk CHECK (hit_count >= 0)
);

-- Opt-in, read-only, immutable shares of a grimoire-chat transcript. Minted
-- SERVER-SIDE from the stored, server-authoritative transcript, never from
-- client-supplied content. The id is an opaque CSPRNG token, so a share url is
-- unguessable. source_session_id is forensics-only and NOT a cascading FK: a
-- snapshot must OUTLIVE its session (ON DELETE SET NULL).
CREATE TABLE grimoire_chat.shared_snapshots (
    id                TEXT PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    transcript        JSONB NOT NULL,
    message_count     INTEGER NOT NULL DEFAULT 0,
    source_session_id TEXT
        REFERENCES grimoire_chat.sessions(id) ON DELETE SET NULL,
    CONSTRAINT shared_snapshots_message_count_nonneg_chk CHECK (message_count >= 0)
);

CREATE INDEX shared_snapshots_created ON grimoire_chat.shared_snapshots (created_at);

-- Role creation: in production the role is created by CNPG (spec.managed.roles,
-- which runs as a superuser); public_writer already exists from the chat_public
-- migration in a real cluster. The DO block is a no-op there and only matters for
-- the CNPG-less test Postgres (which applies every migration as a superuser), so
-- these GRANTs can be exercised even if this migration is applied in isolation.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'public_writer') THEN
        CREATE ROLE public_writer NOLOGIN;
    END IF;
END $$;

-- DML scoped strictly to the grimoire_chat schema (identical posture to
-- chat_public). ALTER DEFAULT PRIVILEGES covers tables a future migration adds to
-- this schema, so the grant does not silently rot. The role is never granted any
-- other schema.
GRANT USAGE ON SCHEMA grimoire_chat TO public_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA grimoire_chat TO public_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA grimoire_chat TO public_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA grimoire_chat
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO public_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA grimoire_chat
    GRANT USAGE, SELECT ON SEQUENCES TO public_writer;

-- public_reader needs schema USAGE plus table SELECT on shared_snapshots ONLY, so
-- the public read route can serve shared chat snapshots (same pattern as
-- 20260620000000 + 20260621130000 for chat_public). USAGE on the schema does NOT
-- expose sessions, messages, or the response cache: public_reader holds table
-- SELECT only on shared_snapshots, so transcripts, IP/UA hashes, and the cache
-- stay unreadable.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'public_reader') THEN
        CREATE ROLE public_reader NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA grimoire_chat TO public_reader;
GRANT SELECT ON grimoire_chat.shared_snapshots TO public_reader;
