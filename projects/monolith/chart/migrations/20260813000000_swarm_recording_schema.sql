CREATE SCHEMA IF NOT EXISTS swarm;

CREATE TABLE swarm.swarm_task (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    task_text TEXT NOT NULL,
    repo TEXT,
    base_branch TEXT,
    conductor_model TEXT NOT NULL,
    budget_usd DOUBLE PRECISION,
    workflow_id TEXT,
    session_id INTEGER,
    settled_at TIMESTAMPTZ
);

CREATE INDEX swarm_task_workflow_id_idx ON swarm.swarm_task (workflow_id);
CREATE INDEX swarm_task_session_id_idx ON swarm.swarm_task (session_id);

CREATE TABLE swarm.swarm_plan_version (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES swarm.swarm_task (id),
    version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    op TEXT NOT NULL,
    author_kind TEXT NOT NULL,
    author TEXT NOT NULL,
    change_json TEXT NOT NULL,
    cause_kind TEXT NOT NULL,
    cause_ref TEXT,
    stated_reason TEXT,
    CONSTRAINT swarm_plan_version_task_version_key UNIQUE (task_id, version)
);

CREATE INDEX swarm_plan_version_task_id_idx ON swarm.swarm_plan_version (task_id);

CREATE TABLE swarm.swarm_plan_node (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES swarm.swarm_task (id),
    node_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    prompt TEXT NOT NULL,
    model TEXT,
    deps_json TEXT NOT NULL,
    max_cost_usd DOUBLE PRECISION NOT NULL,
    side_effects BOOLEAN NOT NULL,
    max_attempts INTEGER,
    turn_timeout_seconds INTEGER,
    created_in_version INTEGER NOT NULL,
    discarded_in_version INTEGER,
    cancelled_in_version INTEGER,
    armed_at TIMESTAMPTZ,
    base_artifact_sha TEXT,
    CONSTRAINT swarm_plan_node_task_node_key UNIQUE (task_id, node_key)
);

CREATE INDEX swarm_plan_node_task_id_idx ON swarm.swarm_plan_node (task_id);

CREATE TABLE swarm.swarm_conductor_call (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES swarm.swarm_task (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    conductor_model TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    refusal_code TEXT,
    version_before INTEGER,
    version_after INTEGER,
    latency_ms INTEGER
);

CREATE INDEX swarm_conductor_call_task_id_idx ON swarm.swarm_conductor_call (task_id);
