-- Drop the isolated repo-doc store (#3905). The reconcile job that populated
-- knowledge.repo_docs / knowledge.repo_doc_chunks was removed in 66eb33979, so
-- both tables are dead derived state: nothing inserts a row, and the repo arm of
-- public_api.knowledge_chunks can never return one. Recreating them is a pure
-- re-run of 20260619000000_repo_docs.sql, retained in git history.
--
-- Order matters. The view reads both tables, so it is first replaced with the
-- notes-only definition from 20260618230000_public_api_chunks.sql (identical
-- column names, types and order, so CREATE OR REPLACE is valid and the
-- public_reader GRANT survives), and only then are the tables dropped.

CREATE OR REPLACE VIEW public_api.knowledge_chunks AS
    SELECT
        n.note_id        AS note_id,
        n.title          AS title,
        c.chunk_index    AS chunk_index,
        c.section_header AS section_header,
        c.chunk_text     AS chunk_text,
        c.embedding      AS embedding
    FROM knowledge.chunks c
    JOIN knowledge.notes n ON c.note_fk = n.id
    WHERE n.visibility = 'public'
      AND n.deleted_at IS NULL;

GRANT SELECT ON public_api.knowledge_chunks TO public_reader;

DROP TABLE IF EXISTS knowledge.repo_doc_chunks;
DROP TABLE IF EXISTS knowledge.repo_docs;
