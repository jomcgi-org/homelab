-- The model an attempt actually ran on, so escalation outcomes can be grouped
-- by model and task class without parsing pin_json. Existing rows take it from
-- their pin, which has always recorded the dispatched model.
ALTER TABLE swarm.swarm_node_run ADD COLUMN model TEXT;
UPDATE swarm.swarm_node_run
SET model = pin_json::jsonb ->> 'model'
WHERE model IS NULL AND pin_json IS NOT NULL;
