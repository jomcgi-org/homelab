-- The worker reaper connects with its own login (factory_reaper). It reads
-- which attempts are open and records worker-fence evidence; it can change
-- nothing else in the connector's state.
GRANT USAGE ON SCHEMA public TO factory_reaper;
GRANT SELECT (id, context_id, state) ON delivery_attempt TO factory_reaper;
GRANT SELECT, INSERT, UPDATE ON worker_fence TO factory_reaper;
