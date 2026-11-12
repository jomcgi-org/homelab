-- DDL only: the jobs role records counts and an optional selector digest.
-- No raw session ids, stored ip hashes or transcript content belong here.
CREATE TABLE grimoire_chat.purge_audit (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    action            TEXT NOT NULL,
    selector_sha256   TEXT,
    sessions_deleted  INTEGER NOT NULL DEFAULT 0,
    messages_deleted  INTEGER NOT NULL DEFAULT 0,
    snapshots_deleted INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT purge_audit_action_chk
        CHECK (action IN ('retention', 'takedown_session', 'takedown_ip_hash')),
    CONSTRAINT purge_audit_selector_chk
        CHECK (selector_sha256 IS NULL OR selector_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT purge_audit_sessions_nonneg_chk CHECK (sessions_deleted >= 0),
    CONSTRAINT purge_audit_messages_nonneg_chk CHECK (messages_deleted >= 0),
    CONSTRAINT purge_audit_snapshots_nonneg_chk CHECK (snapshots_deleted >= 0)
);

-- The schema's default privileges grant public_writer table DML and sequence
-- USAGE/SELECT. Audit evidence is private to the owner/jobs role. Strip table
-- and sequence access explicitly from both restricted roles and PUBLIC.
REVOKE ALL ON grimoire_chat.purge_audit FROM public_writer, public_reader, PUBLIC;
REVOKE ALL ON SEQUENCE grimoire_chat.purge_audit_id_seq
    FROM public_writer, public_reader, PUBLIC;
