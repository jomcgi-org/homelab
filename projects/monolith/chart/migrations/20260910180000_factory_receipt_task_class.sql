-- ADR agents/038 task class: verification mode and implementer floor (#6002).
-- The class is part of receipt identity, so an issue a refine pass moved to
-- agent-ready is received again as a delivery inside the same generation.
ALTER TABLE swarm.factory_receipt
  ADD COLUMN task_class TEXT NOT NULL DEFAULT 'bug-fix';
ALTER TABLE swarm.factory_receipt
  DROP CONSTRAINT factory_receipt_repo_issue_generation_key;
ALTER TABLE swarm.factory_receipt
  ADD CONSTRAINT factory_receipt_repo_issue_generation_class_key
  UNIQUE (repo, issue_number, generation, task_class);
