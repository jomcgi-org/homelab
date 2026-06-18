-- Phase 4a (ADR 005): public chunk-embedding view for public-chat retrieval.
-- Confinement is a DATABASE property, never a prompt rule: the view exposes ONLY
-- the chunk embeddings of public, non-deleted notes, joined to knowledge.notes on
-- the same visibility predicate the other public_api views use. The public chat
-- retrieval path embeds the live user query and runs a pgvector cosine search over
-- this view as the read-only public_reader role (on the read replica), so a private
-- note's chunks are physically unreadable regardless of any jailbreak. The model has
-- no tools and the retrieved text is injected as delimited reference data.
--
-- Like the other public_api views, this is NOT security_invoker: it reads the
-- knowledge schema with the view owner's (app) privileges, while public_reader gets
-- SELECT on the view only and no access to the knowledge schema. A public note whose
-- visibility flips to private (or is soft-deleted) drops out of the view, and its
-- chunks with it, immediately.

CREATE VIEW public_api.knowledge_chunks AS
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
