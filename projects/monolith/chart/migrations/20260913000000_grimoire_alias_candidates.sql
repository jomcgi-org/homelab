-- Durable, private-tier review ledger for ADR services/014 alias merges.
-- Entity ids are intentionally not foreign keys: the merged candidate remains
-- inspectable after the short-name twin is removed.
CREATE TABLE grimoire.alias_candidate (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    short_entity_id     UUID NOT NULL,
    full_entity_id      UUID NOT NULL,
    entity_type         TEXT NOT NULL,
    source_book         TEXT NOT NULL,
    short_name          TEXT NOT NULL,
    full_name           TEXT NOT NULL,
    short_site          TEXT,
    full_site           TEXT,
    short_temporality   TEXT,
    full_temporality    TEXT,
    signal_version      TEXT NOT NULL,
    evidence            JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_count      INTEGER NOT NULL DEFAULT 0,
    state_hash          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'approved', 'rejected', 'stale', 'merged')),
    survivor_entity_id  UUID,
    approved_state_hash TEXT,
    approved_by         TEXT,
    approved_at         TIMESTAMPTZ,
    rejected_state_hash TEXT,
    rejected_by         TEXT,
    rejected_at         TIMESTAMPTZ,
    reopened_by         TEXT,
    reopened_at         TIMESTAMPTZ,
    merged_at           TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (short_entity_id, full_entity_id)
);

CREATE INDEX idx_grimoire_alias_candidate_review
    ON grimoire.alias_candidate (status, updated_at DESC);
