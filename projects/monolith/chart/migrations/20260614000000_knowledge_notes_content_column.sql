-- ADR 006: make Postgres the authoritative store for note bodies.
-- Adds a nullable content column holding the authored markdown body
-- (frontmatter stripped). Existing rows are backfilled out-of-band by the
-- reconciler's one-shot content backfill (reading bodies from disk), NOT via
-- SQL data in this migration: the migrations ConfigMap is applied client-side
-- and capped at 256 KiB of last-applied-configuration, so bulk data must stay
-- out of it.
ALTER TABLE knowledge.notes
  ADD COLUMN content text NULL;
