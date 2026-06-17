-- ADR 006 Phase 4d: drop knowledge.raw_inputs.content.
-- Raw markdown bodies now live in object storage at
-- s3://knowledge/raws/<content_hash>.md (see knowledge/raw_store.py); the
-- row retains content_hash as the lookup key. The S3-completeness gate
-- verified every extant raw_id was uploaded (1737 rows, 0 missing) before
-- this column was dropped, so no body is lost.
ALTER TABLE knowledge.raw_inputs DROP COLUMN IF EXISTS content;
