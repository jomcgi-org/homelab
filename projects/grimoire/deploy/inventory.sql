-- Run with psql -XqAt -v ON_ERROR_STOP=1 against a quiescent, scoped copy.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL TIME ZONE 'UTC';
SET LOCAL search_path = pg_catalog;
SET LOCAL extra_float_digits = 3;

-- Digest every row, including identity keys and membership mappings. Hashing
-- first keeps restricted text out of the inventory and bounds sort-key size.
SELECT format(
  'SELECT %L, count(*), md5(string_agg(h, '''' ORDER BY h COLLATE "C")) FROM
   (SELECT md5(to_jsonb(t)::text) h FROM %I.%I t) rows',
  format('%I.%I', schemaname, tablename), schemaname, tablename)
FROM pg_tables
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY schemaname, tablename
\gexec

SELECT format('SELECT %L, last_value, is_called FROM %I.%I',
              format('%I.%I', schemaname, sequencename), schemaname, sequencename)
FROM pg_sequences ORDER BY schemaname, sequencename
\gexec

SELECT extversion FROM pg_extension WHERE extname = 'vector';
SELECT n.nspname, c.relname, i.indisvalid, i.indisready, pg_get_indexdef(c.oid)
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_am a ON a.oid = c.relam
WHERE a.amname IN ('hnsw', 'ivfflat') ORDER BY n.nspname, c.relname;

SELECT conrelid::regclass, conname, convalidated, pg_get_constraintdef(oid)
FROM pg_constraint WHERE contype = 'f' ORDER BY conrelid::regclass::text, conname;
COMMIT;
