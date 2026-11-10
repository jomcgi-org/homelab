-- Manual reveals and approved-sheet knowledge contribute independently.
-- knowledge_grant remains the effective projection consumed by read routes.
CREATE TABLE grimoire.knowledge_contribution (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    grant_id UUID NOT NULL REFERENCES grimoire.knowledge_grant(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    source_sheet_version_id UUID REFERENCES grimoire.character_sheet_version(id),
    grant_scope TEXT NOT NULL,
    revealed_details JSONB,
    granted_in_session UUID,
    CONSTRAINT knowledge_contribution_source_key UNIQUE (grant_id, source_key),
    CONSTRAINT knowledge_contribution_scope_chk CHECK (grant_scope IN ('full', 'partial', 'name_only'))
);

INSERT INTO grimoire.knowledge_contribution
    (grant_id, source_key, grant_scope, revealed_details, granted_in_session)
SELECT id, 'manual', grant_scope, revealed_details, granted_in_session
FROM grimoire.knowledge_grant;

-- Private campaign data must never inherit public corpus privileges.
REVOKE ALL ON grimoire.knowledge_contribution FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'public_reader') THEN
        REVOKE ALL ON grimoire.knowledge_contribution FROM public_reader;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'public_writer') THEN
        REVOKE ALL ON grimoire.knowledge_contribution FROM public_writer;
    END IF;
END $$;
