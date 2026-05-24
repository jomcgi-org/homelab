-- Soft-delete support for /private/review audit "delete" action.
-- Items with deleted_at IS NOT NULL are excluded from all user-facing
-- read paths (review-queue, knowledge graph, public graph, search,
-- task lists, get-by-id). Undelete clears the column.
--
-- For notes, the on-disk file is moved to _trash/<ts>-<slug>.md
-- (which the gardener and raw-ingest scanners both skip). The original
-- vault-relative path is captured in `pre_delete_path` so undelete
-- can move the file back to exactly where it lived before, without
-- parsing the trash filename. `pre_delete_path` is NULL for non-
-- deleted rows.
--
-- For gaps, the on-disk stub at `_researching/<slug>.md` is hard-
-- deleted on soft-delete (`gaps._remove_stub_if_present`); the
-- gardener regenerates it lazily on the next discover cycle once
-- the gap is undeleted (or skips it if the wikilink is gone).
--
-- No TTL / auto-purge of _trash/ — out of scope; the user explicitly
-- said "no cleanup required for now." When that lands it can be a
-- separate scheduled job.

ALTER TABLE knowledge.gaps
    ADD COLUMN deleted_at timestamptz;

ALTER TABLE knowledge.notes
    ADD COLUMN deleted_at timestamptz;

ALTER TABLE knowledge.notes
    ADD COLUMN pre_delete_path text;
