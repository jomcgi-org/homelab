ALTER TABLE knowledge.notes
    ADD COLUMN revision bigint NOT NULL DEFAULT 0;

CREATE TABLE knowledge.review_outcomes (
    id bigserial PRIMARY KEY,
    note_id text NOT NULL,
    status text NOT NULL,
    reason text NOT NULL,
    note_revision bigint NOT NULL,
    attempts integer NOT NULL DEFAULT 1,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence_observed_at timestamptz,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    next_attempt_at timestamptz,
    CONSTRAINT review_outcomes_status_chk CHECK (
        status IN ('success', 'failed', 'unavailable', 'unsupported')
    )
);

CREATE INDEX ix_knowledge_review_outcomes_note_id
    ON knowledge.review_outcomes (note_id, id DESC);
