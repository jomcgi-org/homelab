-- Grimoire extraction v2: version the extraction prompt by an explicit label
-- instead of a sha256 of the prompt text (spec #1, prompt-versioning framework).
--
-- The chunk_extraction marker key becomes (chunk_id, model, prompt_version): the
-- LABEL (v1, v2, ...) rather than prompt_hash. Promoting a prompt is then a
-- deliberate pointer move (extract.ACTIVE_PROMPT_VERSION / GRIMOIRE_PROMPT_VERSION),
-- not an accidental byte-diff, and a frozen-hash test pins each released prompt.
--
-- Existing rows were extracted under the original (v1) prompt, so they backfill
-- to 'v1'. The column is added, backfilled, and made NOT NULL before the primary
-- key is swapped so no row is ever left without a key component. prompt_hash is
-- dropped: it is no longer part of the key and nothing reads it.
ALTER TABLE grimoire.chunk_extraction
    ADD COLUMN prompt_version TEXT;

UPDATE grimoire.chunk_extraction
    SET prompt_version = 'v1'
    WHERE prompt_version IS NULL;

ALTER TABLE grimoire.chunk_extraction
    ALTER COLUMN prompt_version SET NOT NULL;

ALTER TABLE grimoire.chunk_extraction
    DROP CONSTRAINT chunk_extraction_pkey;

ALTER TABLE grimoire.chunk_extraction
    DROP COLUMN prompt_hash;

ALTER TABLE grimoire.chunk_extraction
    ADD PRIMARY KEY (chunk_id, model, prompt_version);
