-- Attribution-only audit trail for explicit personal-scope knowledge searches.
-- Raw queries and returned knowledge are intentionally excluded.

CREATE TABLE knowledge.personal_retrieval_audit (
    id                  BIGSERIAL   PRIMARY KEY,
    principal_subject   TEXT        NOT NULL,
    principal_actor     TEXT        NOT NULL,
    principal_authority TEXT        NOT NULL,
    personal_scope      TEXT        NOT NULL,
    entrypoint          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT personal_retrieval_audit_subject_length_chk
        CHECK (length(principal_subject) BETWEEN 1 AND 512),
    CONSTRAINT personal_retrieval_audit_actor_length_chk
        CHECK (length(principal_actor) <= 4096),
    CONSTRAINT personal_retrieval_audit_scope_length_chk
        CHECK (length(personal_scope) BETWEEN 1 AND 1024),
    CONSTRAINT personal_retrieval_audit_entrypoint_chk
        CHECK (entrypoint IN ('mcp', 'http'))
);

CREATE INDEX personal_retrieval_audit_created_at_idx
    ON knowledge.personal_retrieval_audit (created_at DESC);

GRANT SELECT, INSERT ON knowledge.personal_retrieval_audit TO agents_writer;
GRANT USAGE, SELECT ON SEQUENCE knowledge.personal_retrieval_audit_id_seq
    TO agents_writer;
