-- Phase 5a' (ADR 004): widen the public knowledge-notes view and add a public
-- edges view so the public service (public_reader) can serve
-- /api/knowledge/public/* with no access to the knowledge schema. The edges
-- view exposes only source-public, non-deleted links; the target end is
-- filtered app-side (program decision: graph edges to private notes are
-- filtered in the handler, not the DB).

CREATE OR REPLACE VIEW public_api.knowledge_notes AS
    SELECT
        note_id,
        title,
        type,
        content,
        indexed_at,
        COALESCE(layout_x_public, layout_x) AS layout_x,
        COALESCE(layout_y_public, layout_y) AS layout_y,
        tags,
        aliases,
        path
    FROM knowledge.notes
    WHERE visibility = 'public'
      AND deleted_at IS NULL;

CREATE VIEW public_api.knowledge_note_links AS
    SELECT
        l.id,
        n.note_id AS source,
        l.target_id AS target,
        l.kind,
        l.edge_type
    FROM knowledge.note_links l
    JOIN knowledge.notes n ON l.src_note_fk = n.id
    WHERE n.visibility = 'public'
      AND n.deleted_at IS NULL;

GRANT SELECT ON public_api.knowledge_note_links TO public_reader;
