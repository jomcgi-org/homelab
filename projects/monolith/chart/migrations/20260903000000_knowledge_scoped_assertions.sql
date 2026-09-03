-- Scoped, verifiable assertions for the factory knowledge graph (#5527, ADR agents/063).
ALTER TABLE knowledge.notes
    ADD COLUMN scope              TEXT,
    ADD COLUMN verification_state TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN confidence         REAL,
    ADD COLUMN valid_from         TIMESTAMPTZ,
    ADD COLUMN valid_until        TIMESTAMPTZ,
    ADD COLUMN observed_at        TIMESTAMPTZ,
    ADD CONSTRAINT notes_verification_state_chk CHECK (
        verification_state IN ('legacy','unverified','verified','disputed','invalidated')),
    ADD CONSTRAINT notes_confidence_chk CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1));
CREATE INDEX notes_scope_idx ON knowledge.notes (scope) WHERE deleted_at IS NULL;

CREATE TABLE knowledge.disputes (
    id                 BIGSERIAL PRIMARY KEY,
    note_id            TEXT NOT NULL,           -- stable knowledge.notes.note_id, NOT the row id (reindex is delete-then-insert)
    raw_id             TEXT,                    -- knowledge.raw_inputs.raw_id of the dispute evidence, if any
    reason             TEXT NOT NULL,
    evidence           JSONB NOT NULL DEFAULT '[]'::jsonb,
    reporter_subject   TEXT,
    reporter_authority TEXT,
    reporter_session   TEXT,
    state              TEXT NOT NULL DEFAULT 'open',
    resolution         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ,
    CONSTRAINT disputes_state_chk CHECK (state IN ('open','confirmed','narrowed','superseded','invalidated','rejected'))
);
CREATE INDEX disputes_note_open_idx ON knowledge.disputes (note_id) WHERE state = 'open';
