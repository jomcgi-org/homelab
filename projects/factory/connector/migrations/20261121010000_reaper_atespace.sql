GRANT SELECT (kagent_namespace) ON delivery_attempt TO factory_reaper;

-- Start scoped observations empty. Previous chart revisions continue to
-- read and write worker_fence during rolling upgrades and rollbacks.
CREATE TABLE scoped_worker_fence (
    LIKE worker_fence INCLUDING ALL,
    FOREIGN KEY (attempt_id) REFERENCES delivery_attempt(id)
);
GRANT SELECT, INSERT, UPDATE ON scoped_worker_fence TO factory_reaper;
