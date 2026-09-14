CREATE TABLE agent_sessions.reservation_reviews (
    permit_id bigint PRIMARY KEY REFERENCES agent_sessions.capacity_reservations(id),
    identity_sha256 text NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    requested_at timestamptz,
    completed_at timestamptz,
    review_session_key text,
    evidence_sha256 text,
    approved_evidence_sha256 text,
    stop_intent_json text,
    guidance text,
    verdict text,
    rationale text,
    state text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0
);

ALTER TABLE claude_agent.routine_reconciliations
    DROP CONSTRAINT routine_reconciliations_disposition_check,
    ADD CONSTRAINT routine_reconciliations_disposition_check
        CHECK (disposition IN ('rearm', 'retain_applied', 'stop'));
