ALTER TABLE swarm.work_item_edge ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE swarm.work_item_edge ADD CONSTRAINT work_item_edge_source_check CHECK (source IN ('manual','github_body','decision'));
