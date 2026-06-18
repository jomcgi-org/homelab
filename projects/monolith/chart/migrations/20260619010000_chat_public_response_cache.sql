-- chat_public.response_cache: a durable, cross-pod cache of public-chat answers
-- (ADR 005 follow-up). Replaces the earlier in-process LRU, which was lost on
-- every pod restart and was not shared once the public web backend runs more
-- than one replica. The backend's HPA is pinned to maxReplicas=1 today only as
-- the GPU-reservation gate; that ceiling is raised after the Phase 6 load test,
-- at which point an in-process cache would diverge per pod. A Postgres-backed
-- cache is durable and shared by every replica.
--
-- A simple key/value table: cache_key is a hash of
-- (normalized_message, prompt_version, notes_watermark), so an identical
-- question hits regardless of trivial whitespace/case, and any change to the
-- system prompt, the model, or the published public notes invalidates the entry
-- because the key changes. The component parts are stored alongside the key for
-- debuggability; touched is the node_touched grounding list replayed on a hit.

CREATE TABLE chat_public.response_cache (
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

-- public_writer already auto-receives DML on new chat_public tables via the
-- ALTER DEFAULT PRIVILEGES set in 20260617030000_chat_public.sql, and this
-- migration runs as the same `app` owner, so the default privileges apply. The
-- explicit grant below is belt-and-braces: it makes the permission obvious at
-- the table that needs it and does not depend on the default-privileges
-- machinery staying intact.
GRANT SELECT, INSERT, UPDATE, DELETE ON chat_public.response_cache TO public_writer;
