-- Run against the hub at the end of the 24-hour frame capture.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH counts AS (
    SELECT tableoid, count(*) AS rows FROM ships.positions GROUP BY tableoid
)
SELECT transaction_timestamp() AS observed_at, p.relid AS partition,
       coalesce(c.rows, 0) AS rows,
       pg_table_size(p.relid) AS partition_bytes,
       pg_indexes_size(p.relid) AS index_bytes
FROM pg_partition_tree('ships.positions') p
LEFT JOIN counts c ON c.tableoid = p.relid
WHERE p.isleaf ORDER BY p.relid::text;
COMMIT;
