-- Grant public_reader read access to the Grimoire corpus tables only (ADR 004
-- security/004 pattern). The public Grimoire tier (Task 2, grimoire/router_public.py)
-- is served by the public tier (monolith-public), which reads monolith-pg-ro as
-- public_reader. Only the corpus itself is public: entity spine + typed detail,
-- knowledge_chunk, chunk_entity_mention, relationship, and embedding. Campaign
-- data (campaign, player_character, game_session, knowledge_grant) stays private,
-- so it is deliberately NOT granted here, unlike the trips pattern's blanket
-- "ALL TABLES IN SCHEMA" grant: only the named corpus tables get SELECT.
--
-- We also deliberately do NOT run ALTER DEFAULT PRIVILEGES here. Because the
-- grimoire schema is mixed (public corpus plus private campaign/grant tables), a
-- schema-wide default grant would auto-expose every future table to
-- public_reader, so a new private table added later would silently become
-- world-readable unless someone remembered to REVOKE. Opt-in is the safe posture
-- for a mixed schema: each future public table adds its own explicit GRANT in
-- its own migration; new tables default to private.

GRANT USAGE ON SCHEMA grimoire TO public_reader;

GRANT SELECT ON
    grimoire.entity,
    grimoire.entity_creature,
    grimoire.entity_spell,
    grimoire.entity_location,
    grimoire.entity_npc,
    grimoire.knowledge_chunk,
    grimoire.chunk_entity_mention,
    grimoire.relationship,
    grimoire.embedding
    TO public_reader;

ALTER DEFAULT PRIVILEGES IN SCHEMA grimoire
    GRANT SELECT ON TABLES TO public_reader;
