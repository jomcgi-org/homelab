-- Default-off repository audit ledger. Repairs remain ordinary open disputes.
CREATE TABLE knowledge.audit_runs (
    id BIGSERIAL PRIMARY KEY,
    job_name TEXT NOT NULL,
    stream TEXT NOT NULL CHECK (stream IN ('scheduled', 'expansion')),
    root_run_id BIGINT REFERENCES knowledge.audit_runs(id),
    depth INTEGER NOT NULL DEFAULT 0,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'prepared',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    sampled_uniform INTEGER NOT NULL DEFAULT 0,
    sampled_weighted INTEGER NOT NULL DEFAULT 0,
    sampled_expansion INTEGER NOT NULL DEFAULT 0,
    metrics JSONB NOT NULL DEFAULT '{}',
    cost_usd DOUBLE PRECISION,
    CONSTRAINT audit_runs_invocation_key UNIQUE (job_name, started_at)
);
CREATE UNIQUE INDEX audit_runs_replay_key ON knowledge.audit_runs(job_name, (metrics ->> 'invocation_key'));
CREATE TABLE knowledge.audit_findings (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES knowledge.audit_runs(id),
    note_id TEXT NOT NULL,
    stream TEXT NOT NULL CHECK (stream IN ('uniform', 'weighted', 'expansion')),
    depth INTEGER NOT NULL DEFAULT 0,
    parent_finding_id BIGINT REFERENCES knowledge.audit_findings(id),
    correctness TEXT NOT NULL DEFAULT 'unknown' CHECK (correctness IN ('holds', 'confirmed', 'narrowed', 'superseded', 'invalidated', 'unknown')),
    clarity TEXT NOT NULL DEFAULT 'unknown' CHECK (clarity IN ('clear', 'unclear', 'unknown')),
    clarity_score DOUBLE PRECISION CHECK (clarity_score >= 0 AND clarity_score <= 1),
    placement TEXT NOT NULL DEFAULT 'unknown' CHECK (placement IN ('ok', 'misplaced', 'unknown')),
    cause TEXT CHECK (cause IN ('lens_overgeneralised', 'missing_supersession', 'stale_after_code_change', 'duplicate_not_merged', 'ranking_surfaced_stale', 'chunking_split_evidence', 'source_wrong', 'other')),
    rationale TEXT NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '[]',
    source_raw_id TEXT,
    source TEXT,
    extraction_version TEXT,
    dispute_id BIGINT REFERENCES knowledge.disputes(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT audit_findings_sample_key UNIQUE (run_id, note_id)
);
CREATE INDEX audit_findings_cooldown_idx ON knowledge.audit_findings(note_id, created_at);
CREATE TABLE knowledge.audit_process_issues (
    id BIGSERIAL PRIMARY KEY,
    cause_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('write_started', 'filed', 'unresolved')),
    marker TEXT NOT NULL,
    issue_number INTEGER,
    defect_count INTEGER NOT NULL DEFAULT 0,
    run_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE knowledge.note_retrievals (
    note_id TEXT NOT NULL,
    day DATE NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (note_id, day)
);
-- The agents MCP tier increments returned-note counters, never the audit ledger.
GRANT INSERT (note_id, day, count), UPDATE (count)
    ON knowledge.note_retrievals TO agents_writer;
