ALTER TABLE swarm.swarm_decision
    DROP CONSTRAINT swarm_decision_kind_check;

ALTER TABLE swarm.swarm_decision
    ADD CONSTRAINT swarm_decision_kind_check
    CHECK (kind IN ('push_gate', 'review_escalation', 'budget'));
