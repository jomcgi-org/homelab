CREATE TABLE swarm.swarm_decision (
    id BIGSERIAL PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    options JSONB NOT NULL,
    note TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ,
    decision TEXT,
    decision_note TEXT,
    actor_subject TEXT,
    actor_authority TEXT,
    CONSTRAINT swarm_decision_kind_check
        CHECK (kind IN ('push_gate', 'review_escalation'))
);

CREATE UNIQUE INDEX swarm_decision_open_idx
    ON swarm.swarm_decision (workflow_id, node_key)
    WHERE decided_at IS NULL;
CREATE INDEX swarm_decision_workflow_requested_idx
    ON swarm.swarm_decision (workflow_id, requested_at);
