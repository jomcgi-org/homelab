-- Durable human intervention inbox for retained distress reports.
CREATE TABLE knowledge.interventions (
    raw_id TEXT PRIMARY KEY REFERENCES knowledge.raw_inputs(raw_id),
    state TEXT NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'acknowledged', 'resolved')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    responder_subject TEXT,
    acknowledged_by_subject TEXT,
    acknowledged_at TIMESTAMPTZ,
    decision_id BIGINT,
    workflow_id TEXT,
    node_key TEXT,
    disposition TEXT CHECK (disposition IS NULL OR disposition IN ('resolved', 'no_action')),
    resolution TEXT,
    resolved_at TIMESTAMPTZ,
    revision INTEGER NOT NULL DEFAULT 1,
    evidence_raw_id TEXT
);

GRANT INSERT (raw_id) ON knowledge.interventions TO agents_writer;
