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
    CONSTRAINT personal_retrieval_audit_authority_chk
        CHECK (principal_authority IN ('standing', 'delegated')),
    CONSTRAINT personal_retrieval_audit_scope_length_chk
        CHECK (length(personal_scope) BETWEEN 1 AND 1024),
    CONSTRAINT personal_retrieval_audit_entrypoint_chk
        CHECK (entrypoint IN ('mcp', 'http'))
);

CREATE INDEX personal_retrieval_audit_created_at_idx
    ON knowledge.personal_retrieval_audit (created_at DESC);

-- Atlas applies migrations with the app role, so this function is owned by app.
-- Definer rights let the agents tier prune atomically without granting DELETE.
CREATE FUNCTION knowledge.prune_personal_retrieval_audit()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    DELETE FROM knowledge.personal_retrieval_audit
    WHERE created_at < pg_catalog.now() - INTERVAL '90 days';
    RETURN NULL;
END;
$$;

REVOKE ALL PRIVILEGES
    ON FUNCTION knowledge.prune_personal_retrieval_audit()
    FROM PUBLIC, agents_writer;

CREATE TRIGGER personal_retrieval_audit_prune_after_insert
AFTER INSERT ON knowledge.personal_retrieval_audit
FOR EACH STATEMENT
EXECUTE FUNCTION knowledge.prune_personal_retrieval_audit();

-- agents_writer inherits SELECT on new knowledge tables from default
-- privileges. Remove every inherited object grant before adding INSERT only.
REVOKE ALL PRIVILEGES
    ON TABLE knowledge.personal_retrieval_audit
    FROM PUBLIC, agents_writer;
GRANT INSERT (
    principal_subject,
    principal_actor,
    principal_authority,
    personal_scope,
    entrypoint
) ON TABLE knowledge.personal_retrieval_audit TO agents_writer;

REVOKE ALL PRIVILEGES
    ON SEQUENCE knowledge.personal_retrieval_audit_id_seq
    FROM PUBLIC, agents_writer;
GRANT USAGE ON SEQUENCE knowledge.personal_retrieval_audit_id_seq TO agents_writer;
