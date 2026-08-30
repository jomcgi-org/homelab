-- Issue #5419 and ADR agents/062 define this dispatch ledger as the durable
-- record that the orchestrator reconciles against the mutable plan graph. It
-- makes the discard operation's "armed" check survive process restarts.

CREATE TABLE swarm.swarm_node_run (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES swarm.swarm_task (id),
    node_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    session_id INTEGER,
    status TEXT NOT NULL,
    cost_usd DOUBLE PRECISION,
    base_sha TEXT,
    head_sha TEXT,
    outcome_json TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CONSTRAINT swarm_node_run_task_node_attempt_key
        UNIQUE (task_id, node_key, attempt)
);

CREATE INDEX swarm_node_run_task_id_idx
    ON swarm.swarm_node_run (task_id);
