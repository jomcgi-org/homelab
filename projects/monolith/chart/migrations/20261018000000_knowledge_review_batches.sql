-- Bounded sessions retain an independent claim/evidence receipt per fact.
CREATE TABLE knowledge.fact_review_batches (
    invocation_key TEXT PRIMARY KEY,
    member_keys JSONB NOT NULL DEFAULT '[]',
    repo_revision TEXT,
    observed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'prepared',
    response JSONB,
    metrics JSONB NOT NULL DEFAULT '{}',
    result JSONB NOT NULL DEFAULT '{}'
);
ALTER TABLE knowledge.fact_review_runs ADD COLUMN completed_at TIMESTAMPTZ;
CREATE INDEX fact_review_runs_pending_idx
    ON knowledge.fact_review_runs(note_id, revision) WHERE status = 'prepared';
CREATE INDEX fact_review_runs_completed_idx
    ON knowledge.fact_review_runs(completed_at) WHERE completed_at IS NOT NULL;
CREATE INDEX fact_review_batches_created_idx
    ON knowledge.fact_review_batches(created_at);
