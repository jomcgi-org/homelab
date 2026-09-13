-- ADR services/014: resumable evidence verification and review-gated aliases.
CREATE TABLE grimoire.entity_verification (
    entity_id         UUID NOT NULL REFERENCES grimoire.entity(id) ON DELETE CASCADE,
    verifier_version  TEXT NOT NULL,
    model             TEXT NOT NULL,
    status            TEXT NOT NULL,
    evidence_chunk_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    corrections       JSONB NOT NULL DEFAULT '[]'::jsonb,
    verified_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, verifier_version),
    CONSTRAINT entity_verification_status_chk
      CHECK (status IN ('verified', 'corrected', 'unverifiable'))
);

CREATE INDEX entity_verification_version_status_idx
  ON grimoire.entity_verification (verifier_version, status);

-- These ids deliberately are not foreign keys. The row is the durable human
-- review and merge audit record, so it must survive deletion of the twin.
CREATE TABLE grimoire.entity_alias_review (
    survivor_id       UUID NOT NULL,
    twin_id           UUID NOT NULL,
    evidence_chunk_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    status            TEXT NOT NULL DEFAULT 'pending',
    reviewed_by       TEXT,
    review_note       TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at       TIMESTAMPTZ,
    merged_at         TIMESTAMPTZ,
    PRIMARY KEY (survivor_id, twin_id),
    CONSTRAINT entity_alias_review_status_chk
      CHECK (status IN ('pending', 'approved', 'rejected', 'merged')),
    CONSTRAINT entity_alias_review_distinct_chk CHECK (survivor_id <> twin_id),
    CONSTRAINT entity_alias_review_approval_chk CHECK (
      status = 'pending' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
    ),
    CONSTRAINT entity_alias_review_merged_chk CHECK (
      status <> 'merged' OR merged_at IS NOT NULL
    )
);

CREATE INDEX entity_alias_review_status_created_idx
  ON grimoire.entity_alias_review (status, created_at);
