-- #7030: requires the maintenance-window sequence in home/README.md.
-- Old readers/writers must be stopped before Atlas applies this migration.
-- New writers cannot insert into the original NOT NULL/no-default schema.
-- Publishing this file makes it eligible for Atlas; comments are not a gate.
ALTER TABLE home.cluster_snapshot DROP COLUMN IF EXISTS alerts RESTRICT;
