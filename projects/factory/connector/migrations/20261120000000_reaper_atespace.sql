GRANT SELECT (kagent_namespace) ON delivery_attempt TO factory_reaper;

-- Older reapers can still be running during rollout. Move the table so
-- they cannot write name-only observations after the migration commits.
ALTER TABLE worker_fence RENAME TO scoped_worker_fence;
DELETE FROM scoped_worker_fence f USING delivery_attempt a
WHERE a.id = f.attempt_id AND a.state IN ('sending', 'accepted', 'ambiguous');
