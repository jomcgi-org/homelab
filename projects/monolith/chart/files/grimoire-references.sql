-- Run with psql -Xq -v ON_ERROR_STOP=1 -v snapshot=<exported snapshot>.
-- Writes, as CSV, each object location that a Grimoire row references in the
-- snapshot, so the references match a pg_dump --snapshot of that snapshot:
--   image_ref,<stored value>  a chunk image, a full s3:// URI
--   upload,<key>              an uploaded handout, a key in the Grimoire bucket
-- A handout that shows a chunk image references that chunk's image_ref, so the
-- first query covers it. grimoire/migration.py reads this file.
SET client_encoding = 'UTF8';
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET TRANSACTION SNAPSHOT :'snapshot';
COPY (
  SELECT 'image_ref', image_ref
  FROM grimoire.knowledge_chunk
  WHERE image_ref IS NOT NULL
  UNION ALL
  SELECT 'upload', body -> 'image' ->> 'key'
  FROM grimoire.session_event
  WHERE kind = 'handout'
    AND body -> 'image' ->> 'source' = 'upload'
    AND jsonb_typeof(body -> 'image' -> 'key') = 'string'
) TO STDOUT WITH (FORMAT csv);
COMMIT;
