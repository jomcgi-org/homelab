-- ADR 006: make Postgres the authoritative store for note bodies.
-- Adds a nullable content column holding the authored markdown body
-- (frontmatter stripped). Existing rows are backfilled out-of-band by the
-- reconciler's one-shot content backfill (reading bodies from disk), NOT via
-- SQL data in this migration: the migrations ConfigMap is applied client-side
-- and capped at 256 KiB of last-applied-configuration, so bulk data must stay
-- out of it.
--
-- Renumbered from 20260614000000 (2026-06-15): the original version sorted
-- before migrations that had already applied (stars-v2, chat-blobs, ships),
-- so Atlas flagged it non-linear and refused to apply it, wedging the whole
-- migration directory. This timestamp sorts after the latest applied
-- migration so the history is linear again. IF NOT EXISTS keeps it safe if a
-- prior partial apply ever created the column.
ALTER TABLE knowledge.notes
  ADD COLUMN IF NOT EXISTS content text NULL;
