-- Resumable post-extraction verifier markers for ADR services/014.
-- A marker is written in the same transaction as any field corrections.
CREATE TABLE grimoire.entity_verification (
    entity_id       UUID NOT NULL REFERENCES grimoire.entity(id) ON DELETE CASCADE,
    verifier_version TEXT NOT NULL,
    status          TEXT NOT NULL,
    result          JSONB NOT NULL,
    verified_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, verifier_version),
    CONSTRAINT entity_verification_status_chk CHECK (
        status IN ('verified', 'corrected', 'unverifiable')
    )
);
