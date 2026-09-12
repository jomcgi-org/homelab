ALTER TABLE swarm.swarm_task
  ADD COLUMN task_class TEXT NOT NULL DEFAULT 'bug-fix',
  ADD COLUMN capability_tier TEXT NOT NULL DEFAULT 'small',
  ADD CONSTRAINT swarm_task_capability_tier_check
    CHECK (capability_tier IN ('small', 'opus')),
  ADD CONSTRAINT swarm_task_judgment_floor_check
    CHECK (task_class != 'judgment-analysis' OR capability_tier = 'opus');

CREATE INDEX swarm_task_task_class_idx ON swarm.swarm_task (task_class);
CREATE INDEX swarm_task_capability_tier_idx ON swarm.swarm_task (capability_tier);

ALTER TABLE swarm.factory_receipt
  ADD COLUMN source_key TEXT,
  ADD COLUMN requires_issue_close BOOLEAN NOT NULL DEFAULT TRUE;

UPDATE swarm.factory_receipt
   SET source_key = 'issue:' || issue_number
 WHERE source_key IS NULL;

ALTER TABLE swarm.factory_receipt
  ALTER COLUMN source_key SET NOT NULL,
  DROP CONSTRAINT factory_receipt_repo_issue_generation_class_key,
  ADD CONSTRAINT factory_receipt_repo_generation_source_class_key
    UNIQUE (repo, generation, source_key, task_class);
