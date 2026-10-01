-- chat_public.purge_audit: one row per retention run or takedown (ADR 005).
--
-- DDL only. Every delete runs from the jobs image (chat_public/retention.py);
-- this table records what each run removed so a purge is auditable. It holds
-- counts and a sha256 digest of the takedown selector only: no session id, no
-- ip_hash, no transcript text, no PII. selector_sha256 is NULL for retention.

CREATE TABLE chat_public.purge_audit (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    action            TEXT NOT NULL,
    selector_sha256   TEXT,
    sessions_deleted  INTEGER NOT NULL DEFAULT 0,
    messages_deleted  INTEGER NOT NULL DEFAULT 0,
    snapshots_deleted INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT purge_audit_action_chk
        CHECK (action IN ('retention', 'takedown_session', 'takedown_ip_hash')),
    CONSTRAINT purge_audit_sessions_nonneg_chk CHECK (sessions_deleted >= 0),
    CONSTRAINT purge_audit_messages_nonneg_chk CHECK (messages_deleted >= 0),
    CONSTRAINT purge_audit_snapshots_nonneg_chk CHECK (snapshots_deleted >= 0)
);

-- ALTER DEFAULT PRIVILEGES in this schema auto-grants public_writer DML on every
-- new table and USAGE/SELECT on new sequences. The internet-facing role must not
-- read or rewrite the audit, so strip those grants (and public_reader's) here.
REVOKE ALL ON chat_public.purge_audit FROM public_writer, public_reader;
REVOKE ALL ON SEQUENCE chat_public.purge_audit_id_seq FROM public_writer;
