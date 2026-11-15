CREATE TABLE recovery_hold (
    id bigserial PRIMARY KEY,
    target_attempt_id bigint NOT NULL REFERENCES delivery_attempt(id),
    work_item_id bigint NOT NULL REFERENCES work_item(id),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    snapshot jsonb NOT NULL,
    reason text NOT NULL,
    not_before timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'attested', 'invalidated', 'consumed')),
    action text CHECK (action IN ('retry', 'cancel')),
    attested_by text,
    evidence text,
    attested_at timestamptz,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((action IS NULL) = (attested_at IS NULL)),
    CHECK (state NOT IN ('attested', 'consumed') OR
        (action IS NOT NULL AND attested_by IS NOT NULL AND evidence IS NOT NULL
         AND btrim(attested_by) <> '' AND btrim(evidence) <> ''))
);
CREATE UNIQUE INDEX recovery_hold_active_fingerprint ON recovery_hold (fingerprint)
    WHERE state IN ('pending', 'attested');
CREATE UNIQUE INDEX recovery_hold_active_target ON recovery_hold (target_attempt_id)
    WHERE state IN ('pending', 'attested');
CREATE INDEX recovery_hold_work ON recovery_hold (work_item_id, id);
