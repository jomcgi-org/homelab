-- Refill recurrence preserves real issue and generation identity (#7376).
ALTER TABLE swarm.factory_receipt
    ADD COLUMN recurrence_key text NOT NULL DEFAULT '';
ALTER TABLE swarm.factory_receipt
    DROP CONSTRAINT factory_receipt_repo_issue_generation_class_key;
ALTER TABLE swarm.factory_receipt
    ADD CONSTRAINT factory_receipt_repo_issue_generation_class_key
    UNIQUE (repo, issue_number, generation, task_class, recurrence_key);
