-- Tests for the migration-destructive-ddl semgrep rule.
--
-- Destructive DDL (DROP TABLE, DROP SCHEMA, TRUNCATE) in migrations is
-- irreversible and can cause permanent data loss. The rule flags these so
-- they are reviewed; suppress with:
--   -- nosemgrep: migration-destructive-ddl
-- and a comment explaining why the operation is safe.
--
-- Annotations (SQL comment syntax):
--   -- ruleid: migration-destructive-ddl  the next non-annotation line MUST be flagged
--   -- ok: migration-destructive-ddl      the next non-annotation line MUST NOT be flagged

-- Positive examples (should be flagged)

-- ruleid: migration-destructive-ddl
DROP TABLE IF EXISTS some_schema.stale_table;

-- ruleid: migration-destructive-ddl
DROP TABLE legacy_table;

-- ruleid: migration-destructive-ddl
DROP SCHEMA IF EXISTS old_schema CASCADE;

-- ruleid: migration-destructive-ddl
TRUNCATE some_schema.ephemeral_results;

-- ruleid: migration-destructive-ddl
truncate some_schema.other_table;

-- Negative examples (should not be flagged)

-- ok: migration-destructive-ddl
CREATE TABLE IF NOT EXISTS some_schema.new_table (id SERIAL PRIMARY KEY);

-- ok: migration-destructive-ddl
ALTER TABLE some_schema.existing_table ADD COLUMN new_col TEXT;

-- ok: migration-destructive-ddl
CREATE INDEX CONCURRENTLY ON some_schema.new_table (id);

-- ok: migration-destructive-ddl
ALTER TABLE some_schema.existing_table DROP COLUMN old_col;

-- ok: migration-destructive-ddl
-- nosemgrep: migration-destructive-ddl (safe: ephemeral table, repopulated immediately)
DROP TABLE IF EXISTS some_schema.safe_to_drop;
