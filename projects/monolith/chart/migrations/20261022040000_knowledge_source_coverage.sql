-- Content-based coverage blocks are inert until the coverage switch is enabled.
-- No existing outcomes or claim rows are changed by this migration.
CREATE TABLE knowledge.fact_review_source_blocks (
    note_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    outcome_id BIGINT NOT NULL,
    identity JSONB NOT NULL,
    released BOOLEAN NOT NULL DEFAULT false,
    next_probe_at TIMESTAMPTZ NOT NULL,
    last_probe_at TIMESTAMPTZ
);
CREATE INDEX fact_review_source_blocks_due_idx
    ON knowledge.fact_review_source_blocks(next_probe_at);
CREATE INDEX fact_review_source_blocks_probed_idx
    ON knowledge.fact_review_source_blocks(last_probe_at);
