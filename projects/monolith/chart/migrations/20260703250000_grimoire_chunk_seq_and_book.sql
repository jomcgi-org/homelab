-- Grimoire UI overhaul, Phase 2: reading-order chunk sequence + book metadata
-- (docs/plans/2026-07-03-grimoire-ui-overhaul.md, Task 2.1).
--
-- The Library and chunk reader need two things the schema does not yet carry:
--   1. A stable reading order per book. created_at is unreliable (bulk upserts
--      share a timestamp; re-uploads mutate rows in place), so add an explicit
--      seq populated from NDJSON line order at ingest.
--   2. Human display names for books. book_id is a slug/UUID today; the Library
--      renames it via grimoire.book.display_name.

-- 1. seq: reading-order position within a book, 0-based, assigned from NDJSON
--    line order by the loader. Nullable (a row exists briefly before the loader
--    sets it, and pre-existing rows are backfilled below).
ALTER TABLE grimoire.knowledge_chunk ADD COLUMN seq INTEGER;

-- Backfill existing chunks: (created_at, chunk_ref) approximates reading order
-- for the already-loaded corpus. A later re-upload rewrites seq from true line
-- order (see ingest._upsert_book_chunks); the risks section of the plan accepts
-- this approximation for the one book loaded before this migration.
WITH ordered AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY book_id ORDER BY created_at, chunk_ref
        ) - 1 AS rn
    FROM grimoire.knowledge_chunk
)
UPDATE grimoire.knowledge_chunk AS kc
SET seq = ordered.rn
FROM ordered
WHERE kc.id = ordered.id;

-- Reading-order lookups (section tree, chunk reader prev/next, paged chunk list)
-- all scan by (book_id, seq); index it so they stay index-ordered as the corpus
-- grows past the seq-scan-is-fine v1 scale.
CREATE INDEX idx_grimoire_knowledge_chunk_book_seq
    ON grimoire.knowledge_chunk (book_id, seq);

-- 2. Book metadata: one row per book_id, display_name defaults to the id until
--    renamed from the Library UI (PATCH /books/{book_id}).
CREATE TABLE grimoire.book (
    id           TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Register the books already present as chunks (display_name defaults to id).
INSERT INTO grimoire.book (id, display_name)
SELECT DISTINCT book_id, book_id FROM grimoire.knowledge_chunk
ON CONFLICT (id) DO NOTHING;
