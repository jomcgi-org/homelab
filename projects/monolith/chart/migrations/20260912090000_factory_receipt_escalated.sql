-- A delivery pause leaves the lane as an escalation, for #6041 and #6002.
-- The receipt settles `escalated` rather than staying admitted behind
-- task_paused, so the slot and its accounting are free while a person
-- decides, and `direction_json` carries the operator's answer into the
-- planner prompt of the task the decision re-admits.
ALTER TABLE swarm.factory_receipt
  DROP CONSTRAINT factory_receipt_state_check;
ALTER TABLE swarm.factory_receipt
  ADD CONSTRAINT factory_receipt_state_check
  CHECK (state IN ('queued', 'admitted', 'uncertain', 'escalated',
                   'succeeded', 'failed', 'cancelled'));
ALTER TABLE swarm.factory_receipt
  ADD COLUMN direction_json TEXT;
