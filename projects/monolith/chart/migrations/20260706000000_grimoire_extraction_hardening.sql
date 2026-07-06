-- Grimoire extraction hardening: two nullable columns that sharpen extraction
-- identity and provenance. Both are metadata-only additions (a nullable column
-- with no default is no table rewrite and no backfill).

-- entity.site: the parent-place key for a location that is its own keyed entry
-- (a dungeon room's containing site), lower-cased. Dedup for locations becomes
-- book-scoped and site-aware, so same-named rooms in different dungeons or books
-- ("Kitchen" is a heading in 11 books) stay distinct instead of merging under
-- the global (entity_type, lower(name)) key. NULL for non-location entities and
-- for prose location references that name no site. Mirror of Entity.site in
-- grimoire/models.py.
ALTER TABLE grimoire.entity
    ADD COLUMN site TEXT;

-- relationship.chunk_id: the knowledge_chunk an edge was first extracted from.
-- Dedup stays on the (from, to, rel_type) triple, so the first writer's chunk
-- wins and a duplicate never overwrites it. Nullable: edges written before this
-- column have NULL. Mirror of Relationship.chunk_id in grimoire/models.py.
ALTER TABLE grimoire.relationship
    ADD COLUMN chunk_id uuid REFERENCES grimoire.knowledge_chunk (id);
