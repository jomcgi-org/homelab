-- Add a copyright flag to grimoire.book so the public tier can gate the
-- full-text Reader to open-licensed books only. The public Library still lists
-- every book (breadth of the corpus is the showcase), and the transformative
-- surfaces (Entities / Chat / Explore) stay corpus-wide; but the verbatim
-- reconstruction path (/books/{id}/read, /chunks/{id}, /chunks/{id}/image)
-- must refuse copyrighted books. See ADR services/012 (public tier posture).
--
-- Default TRUE is the load-bearing safety choice: a newly ingested book is
-- treated as copyrighted (Reader locked) until explicitly classified open, so
-- a forgotten classification fails closed (locked) rather than open (leaked).
-- ingest._upsert_book flips the known open-licensed slugs to FALSE at load
-- time (OPEN_LICENSE_BOOK_IDS), so future BFRD / A5E ingests self-classify.

ALTER TABLE grimoire.book
    ADD COLUMN copyrighted_content boolean NOT NULL DEFAULT true;

-- Backfill the already-loaded open-licensed books. Both System Reference
-- Documents were released by Wizards of the Coast under Creative Commons
-- CC BY 4.0 (SRD 5.1 in 2023, SRD 5.2 in 2025), which permits redistribution
-- with attribution.
UPDATE grimoire.book
    SET copyrighted_content = false
    WHERE id IN (
        'system-reference-doc-5-1',
        'system-reference-doc-5-2'
    );
