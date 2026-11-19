-- Run with psql -XqAt -v ON_ERROR_STOP=1 against a quiescent, scoped copy.
-- On a live source, two optional psql variables scope the read. schemas is a
-- comma-separated list; without it, every schema is read. snapshot is an
-- exported snapshot, so the rows match a pg_dump --snapshot of that snapshot.
\if :{?schemas}
\else
\set schemas ''
\endif
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
\if :{?snapshot}
SET TRANSACTION SNAPSHOT :'snapshot';
\endif
-- GMT is built in, so it needs no time zone files.
SET LOCAL TIME ZONE 'GMT';
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
  AND (:'schemas' = '' OR schemaname::text = ANY (string_to_array(:'schemas', ',')))
ORDER BY schemaname, tablename
\gexec

SELECT format('SELECT %L, last_value, is_called FROM %I.%I',
              format('%I.%I', schemaname, sequencename), schemaname, sequencename)
FROM pg_sequences
WHERE :'schemas' = '' OR schemaname::text = ANY (string_to_array(:'schemas', ','))
ORDER BY schemaname, sequencename
\gexec

SELECT extversion FROM pg_extension WHERE extname = 'vector';
SELECT n.nspname, c.relname, i.indisvalid, i.indisready, pg_get_indexdef(c.oid)
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_am a ON a.oid = c.relam
WHERE a.amname IN ('hnsw', 'ivfflat')
  AND (:'schemas' = '' OR n.nspname::text = ANY (string_to_array(:'schemas', ',')))
ORDER BY n.nspname, c.relname;

SELECT k.conrelid::regclass, k.conname, k.convalidated, pg_get_constraintdef(k.oid)
FROM pg_constraint k JOIN pg_namespace n ON n.oid = k.connamespace
WHERE k.contype = 'f'
  AND (:'schemas' = '' OR n.nspname::text = ANY (string_to_array(:'schemas', ',')))
ORDER BY k.conrelid::regclass::text, k.conname;
COMMIT;
