-- Repo-docs ingest (public-chat grounding). Two ISOLATED tables, deliberately
-- outside the curated knowledge.notes graph so the gardener and gap loop never
-- touch these machine-synced, fully reconstructable rows. The private monolith's
-- knowledge.repo_docs_reconcile job upserts/deletes them from the image-baked
-- manifest by content hash.

CREATE TABLE knowledge.repo_docs (
    id            SERIAL PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    content_hash  TEXT NOT NULL,
    title         TEXT NOT NULL,
    indexed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE knowledge.repo_doc_chunks (
    id              SERIAL PRIMARY KEY,
    repo_doc_fk     INTEGER NOT NULL REFERENCES knowledge.repo_docs(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    section_header  TEXT NOT NULL DEFAULT '',
    chunk_text      TEXT NOT NULL,
    embedding       vector(1024) NOT NULL
);

CREATE INDEX repo_doc_chunks_doc_idx ON knowledge.repo_doc_chunks (repo_doc_fk);

-- HNSW cosine index for parity with knowledge.chunks (chunks_embedding_hnsw), so
-- the repo-doc arm of the UNION-ALL'd view is index-served on cosine retrieval.
CREATE INDEX repo_doc_chunks_embedding_hnsw ON knowledge.repo_doc_chunks USING hnsw (embedding vector_cosine_ops);

-- Surface repo docs to the public-chat retrieval path by UNION-ing them into the
-- existing public chunk view. Same columns/types/order as the original (Phase 4a,
-- 20260618230000_public_api_chunks.sql) so CREATE OR REPLACE is valid and the public_reader GRANT is
-- preserved. Synthetic note_id 'repo:'||path never collides with a real note_id;
-- the retrieval grounding uses title + chunk_text, and the chat graph overlay
-- silently ignores ids with no matching graph node.
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
      AND n.deleted_at IS NULL
    UNION ALL
    SELECT
        'repo:' || d.path AS note_id,
        d.title           AS title,
        rc.chunk_index    AS chunk_index,
        rc.section_header AS section_header,
        rc.chunk_text     AS chunk_text,
        rc.embedding      AS embedding
    FROM knowledge.repo_doc_chunks rc
    JOIN knowledge.repo_docs d ON rc.repo_doc_fk = d.id;

GRANT SELECT ON public_api.knowledge_chunks TO public_reader;
