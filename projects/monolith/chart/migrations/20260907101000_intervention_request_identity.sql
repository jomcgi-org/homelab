-- Preserve accepted request identity and server attribution for operator actions.
-- Existing rows stay NULL where no historical identity was recorded. Do not
-- infer an accepted request revision from a later lifecycle revision.
ALTER TABLE knowledge.interventions
    ADD COLUMN acknowledged_request_revision INTEGER,
    ADD COLUMN decision_state TEXT,
    ADD COLUMN associated_by_subject TEXT,
    ADD COLUMN associated_at TIMESTAMPTZ,
    ADD COLUMN decision_request_revision INTEGER,
    ADD COLUMN resolved_request_revision INTEGER,
    ADD COLUMN evidence_by_subject TEXT,
    ADD COLUMN evidence_submitted_at TIMESTAMPTZ;

COMMENT ON COLUMN knowledge.interventions.decision_state IS
    'Decision state observed through the swarm owner at associated_at, not a live projection.';
