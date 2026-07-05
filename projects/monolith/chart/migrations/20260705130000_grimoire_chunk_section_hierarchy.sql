-- Grimoire extraction v2: full section-ancestry breadcrumb per chunk, used ONLY
-- as extraction context (spec #2 section-hierarchy plumbing).
--
-- section_path stays the 2-level "chapter/leaf" breadcrumb the reader + Chapters
-- nav split on. This new column carries the FULL ancestor path (shallowest
-- first, joined by " > ", e.g. "Chapter 3: Magic Items > Armor > Armor of
-- Vulnerability") so the extraction model sees document nesting (location
-- containment, disambiguating generic leaf headings like "Area 5"). It is NOT
-- embedded, so the chunk loader treats it as metadata: a re-load backfills it in
-- place without re-embedding (see grimoire.ingest._upsert_book_chunks).
--
-- Nullable: chunks loaded before the re-load have NULL and extraction falls back
-- to section_path. Adding a nullable column with no default is a metadata-only
-- change (no table rewrite, no chunk-boundary change, no re-embed).
ALTER TABLE grimoire.knowledge_chunk
    ADD COLUMN section_hierarchy TEXT;
