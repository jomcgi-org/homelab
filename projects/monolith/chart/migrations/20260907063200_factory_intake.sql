CREATE TABLE swarm.factory_control (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'disabled',
    policy_json TEXT NOT NULL DEFAULT '{}',
    admitted_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    stopped_at TIMESTAMPTZ,
    CONSTRAINT factory_control_id_check CHECK (id = 'factory'),
    CONSTRAINT factory_control_state_check
        CHECK (state IN ('disabled', 'enabled', 'paused', 'stopped')),
    CONSTRAINT factory_control_admitted_count_check CHECK (admitted_count >= 0),
    CONSTRAINT factory_control_version_check CHECK (version >= 0)
);

INSERT INTO swarm.factory_control (id, state, policy_json, admitted_count, version, actor)
    VALUES ('factory', 'disabled', '{}', 0, 0, 'bootstrap');

CREATE TABLE swarm.factory_receipt (
    id BIGSERIAL PRIMARY KEY,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    url TEXT NOT NULL,
    actor TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    task_id TEXT REFERENCES swarm.swarm_task (id),
    policy_json TEXT,
    task_paused BOOLEAN NOT NULL DEFAULT FALSE,
    cancellation_requested BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT factory_receipt_issue_number_check CHECK (issue_number > 0),
    CONSTRAINT factory_receipt_generation_check CHECK (generation >= 0),
    CONSTRAINT factory_receipt_state_check
        CHECK (state IN ('queued', 'admitted', 'uncertain', 'succeeded', 'failed', 'cancelled')),
    CONSTRAINT factory_receipt_repo_issue_generation_key
        UNIQUE (repo, issue_number, generation),
    CONSTRAINT factory_receipt_task_id_key UNIQUE (task_id)
);

CREATE INDEX factory_receipt_state_created_at_idx
    ON swarm.factory_receipt (state, created_at);

CREATE TABLE swarm.factory_start (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES swarm.swarm_task (id),
    start_key TEXT NOT NULL,
    actor TEXT NOT NULL,
    model TEXT NOT NULL,
    max_cost_usd DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved',
    cost_usd DOUBLE PRECISION,
    session_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT factory_start_task_start_key UNIQUE (task_id, start_key),
    CONSTRAINT factory_start_status_check
        CHECK (status IN ('reserved', 'succeeded', 'failed', 'uncertain', 'cancelled')),
    CONSTRAINT factory_start_max_cost_check CHECK (max_cost_usd > 0),
    CONSTRAINT factory_start_cost_check CHECK (cost_usd IS NULL OR cost_usd >= 0)
);

CREATE INDEX factory_start_task_status_idx
    ON swarm.factory_start (task_id, status);

CREATE TABLE swarm.factory_audit (
    id BIGSERIAL PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    task_id TEXT REFERENCES swarm.swarm_task (id),
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX factory_audit_created_at_idx
    ON swarm.factory_audit (created_at);
