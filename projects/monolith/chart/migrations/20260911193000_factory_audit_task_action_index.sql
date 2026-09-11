-- Landing reads the audit trail by task and action on every reconciler tick
-- (#6002): the per-task landing state, the once-only fences, and the
-- settlement evidence are all (task_id, action) lookups. The table only had
-- an index on created_at, so each of those was a sequential scan over an
-- append-only trail that grows with every tick.
CREATE INDEX IF NOT EXISTS factory_audit_task_action_idx
  ON swarm.factory_audit (task_id, action);
