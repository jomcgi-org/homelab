-- Grimoire extraction v2: one-off wipe of the DERIVED extraction tables.
--
-- NOT a migration and NOT auto-run: this file is applied by hand exactly once,
-- immediately BEFORE the v2 + V4-Flash re-extraction, to clear the v1 spine so
-- the graph is rebuilt cleanly from the (kept) chunks. Run it against the
-- monolith Postgres, e.g.:
--
--   kubectl -n monolith exec -it monolith-pg-1 -- \
--     psql "$DATABASE_URL" -f - < projects/monolith/grimoire/wipe/truncate_derived_tables.sql
--
-- WHY DELETE, not TRUNCATE: grimoire.entity is referenced by a foreign key from
-- grimoire.knowledge_grant. Postgres refuses to TRUNCATE a table referenced by a
-- FK unless the referencing table is truncated too (or CASCADE is used), and we
-- must NEVER touch knowledge_grant. DELETE respects the FK by row: it succeeds
-- while no grant references an entity (the state before live play), and fails
-- loudly if a grant exists, which is the correct signal to the operator.
--
-- WIPED (all fully regenerable from grimoire.knowledge_chunk):
--   entity, entity_creature, entity_location, entity_npc, entity_spell,
--   chunk_entity_mention, relationship, chunk_extraction
-- KEPT (do not delete): knowledge_chunk, embedding, book, and every campaign /
--   session / grant table (campaign, player_character, game_session,
--   knowledge_grant).
--
-- Order matters: delete the tables that reference grimoire.entity before
-- grimoire.entity itself.
BEGIN;

DELETE FROM grimoire.chunk_entity_mention;
DELETE FROM grimoire.relationship;
DELETE FROM grimoire.entity_creature;
DELETE FROM grimoire.entity_location;
DELETE FROM grimoire.entity_npc;
DELETE FROM grimoire.entity_spell;
-- chunk_extraction markers gate re-extraction: clearing them makes every chunk
-- pending again (belt-and-suspenders with the new v2 prompt_version key, since
-- the v2 label is already unmarked; harmless if the label alone would suffice).
DELETE FROM grimoire.chunk_extraction;
DELETE FROM grimoire.entity;

-- OPTIONAL, operator's choice (left OUT of the transaction body above so the core
-- wipe matches the spec's table list exactly). Re-extraction mints new entity
-- UUIDs and re-embeds them, so the OLD entity-kind embedding rows become
-- orphaned (their embeddable_id no longer resolves). Chunk embeddings are kept
-- (expensive to regenerate). To also drop the now-orphaned entity vectors,
-- uncomment:
--   DELETE FROM grimoire.embedding WHERE embeddable_kind = 'entity';

COMMIT;
