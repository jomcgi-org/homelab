-- Independent source-grounded review. Only the backend owns this ledger.
CREATE TABLE knowledge.fact_review_runs (
    invocation_key TEXT PRIMARY KEY,
    note_id TEXT,
    revision INTEGER,
    content_hash TEXT,
    claim JSONB NOT NULL DEFAULT '{}',
    sources JSONB NOT NULL DEFAULT '[]',
    observed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL DEFAULT 'prepared',
    result JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX fact_review_runs_note_idx ON knowledge.fact_review_runs(note_id, created_at);
