-- Problem-issue reconciliation reads the newest row for a bounded action set.
-- Keep those global lookups on the append-only audit trail indexed by action.
CREATE INDEX IF NOT EXISTS factory_audit_action_id_idx
  ON swarm.factory_audit (action, id);
