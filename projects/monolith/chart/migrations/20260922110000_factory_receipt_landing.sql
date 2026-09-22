-- Approved delivery remains pending until trusted publication and live rollout evidence.
ALTER TABLE swarm.factory_receipt DROP CONSTRAINT factory_receipt_state_check;
ALTER TABLE swarm.factory_receipt ADD CONSTRAINT factory_receipt_state_check
CHECK (state IN ('queued', 'admitted', 'uncertain', 'landing', 'escalated', 'succeeded', 'failed', 'cancelled'));
