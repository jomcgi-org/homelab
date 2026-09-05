-- Manual knowledge extraction bursts are bounded by both job count and time.
-- A count-only grant can become permanent when forgotten before exhaustion,
-- while a time-only grant can drain without a job ceiling. Both extra_jobs and
-- expires_at are therefore required, and runtime enforcement checks both axes.
CREATE TABLE knowledge.kg_burst_grants (
    id         BIGSERIAL PRIMARY KEY,
    extra_jobs INTEGER NOT NULL CHECK (extra_jobs > 0),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL,
    CONSTRAINT kg_burst_grants_expiry_chk CHECK (expires_at > created_at)
);

CREATE INDEX kg_burst_grants_active_idx
    ON knowledge.kg_burst_grants (expires_at DESC, created_at DESC);
