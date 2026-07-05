-- Grimoire extraction v4: generic typed extraction (lore + gameplay + mechanics).
-- Expands the entity_type set from the 7 lore types to ~18 (adds gameplay
-- event/quest and the mechanics types), derives a stored `category` column from
-- entity_type, adds a nullable `temporality` (set only for event/quest), and adds
-- a generic `detail` JSONB for the new types that have no dedicated detail table.
-- creature/location/npc/spell keep their existing typed detail tables unchanged.
-- No data backfill: existing rows compute `category` from their entity_type via
-- the generated column, and fresh v4 extraction populates the rest.

-- Expand the entity_type CHECK to the full v4 taxonomy.
ALTER TABLE grimoire.entity DROP CONSTRAINT entity_entity_type_chk;
ALTER TABLE grimoire.entity ADD CONSTRAINT entity_entity_type_chk CHECK (
    entity_type IN (
        -- lore (unchanged from v1)
        'creature', 'spell', 'location', 'npc', 'faction', 'deity', 'item',
        -- gameplay
        'event', 'quest',
        -- mechanics
        'condition', 'feat', 'race', 'background', 'class', 'subclass',
        'class_feature', 'action', 'rule'
    )
);

-- category is DERIVED from entity_type by a stored generated column, so it is
-- always consistent regardless of write path. spell derives to 'lore'; the
-- mechanics surface unions spell in at query time (category = 'mechanics' OR
-- entity_type = 'spell'), not at the column level.
ALTER TABLE grimoire.entity ADD COLUMN category TEXT
    GENERATED ALWAYS AS (
        CASE
            WHEN entity_type IN (
                'creature', 'spell', 'location', 'npc', 'faction', 'deity', 'item'
            ) THEN 'lore'
            WHEN entity_type IN ('event', 'quest') THEN 'gameplay'
            ELSE 'mechanics'
        END
    ) STORED;
CREATE INDEX idx_grimoire_entity_category ON grimoire.entity (category);

-- temporality is set only for event/quest (nullable everywhere else).
ALTER TABLE grimoire.entity ADD COLUMN temporality TEXT
    CHECK (temporality IS NULL OR temporality IN ('historical', 'present', 'future'));

-- Generic typed-detail payload for the gameplay/mechanics types that have no
-- dedicated detail table. creature/location/npc/spell keep their own tables.
ALTER TABLE grimoire.entity ADD COLUMN detail JSONB;
