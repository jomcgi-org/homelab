-- Grimoire extraction v6: the `table` entity type (mechanics category).
-- A table or list of options (a treasure/magic-item table, a spell list, a class
-- progression, a random-encounter table) is now captured as ONE `table` entity
-- instead of exploding into dozens of junk row entities ("+1 Arrow", a bare
-- "3rd Level" spell-list heading). Metadata-only: this widens the entity_type
-- CHECK by one value; no new column, no data backfill.

-- Expand the entity_type CHECK to add `table`. The live constraint is
-- entity_entity_type_check: the original inline column CHECK in
-- 20260703070000_grimoire_schema.sql was unnamed, so Postgres auto-named it
-- entity_entity_type_check (the "_check" convention), and
-- 20260705150000_grimoire_extraction_v4.sql dropped and re-added it under that
-- same name. Drop and re-add it again with `table` appended to the v4 taxonomy.
ALTER TABLE grimoire.entity DROP CONSTRAINT entity_entity_type_check;
ALTER TABLE grimoire.entity ADD CONSTRAINT entity_entity_type_check CHECK (
    entity_type IN (
        -- lore (unchanged from v1)
        'creature', 'spell', 'location', 'npc', 'faction', 'deity', 'item',
        -- gameplay
        'event', 'quest',
        -- mechanics
        'condition', 'feat', 'race', 'background', 'class', 'subclass',
        'class_feature', 'action', 'rule',
        -- v6: a table/list captured as one entity
        'table'
    )
);

-- `category` needs no change: `table` falls into the GENERATED column's ELSE
-- 'mechanics' branch (added in 20260705150000_grimoire_extraction_v4.sql),
-- mirrored code-side by models._ENTITY_CATEGORY_EXPR.
