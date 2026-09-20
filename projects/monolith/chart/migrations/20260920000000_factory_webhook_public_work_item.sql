-- Durable webhook replay fencing. The row is inserted and committed in the
-- same transaction as the work-item mutation, so a failed handler leaves no
-- fence that could suppress GitHub's legitimate retry.
CREATE TABLE swarm.factory_webhook_delivery (
    delivery_id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    action TEXT,
    repo TEXT NOT NULL,
    issue_number INTEGER,
    outcome TEXT NOT NULL,
    CONSTRAINT factory_webhook_delivery_outcome_check CHECK (
        outcome IN (
            'processing',
            'ignored_event',
            'ignored_action',
            'trusted_minted',
            'trusted_synced',
            'trusted_unchanged',
            'trusted_closed',
            'local_untouched',
            'semi_trusted_held',
            'untrusted_ignored'
        )
    ),
    work_item_id BIGINT REFERENCES swarm.work_item (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX factory_webhook_delivery_issue_created_idx
    ON swarm.factory_webhook_delivery (repo, issue_number, created_at);

-- The public tier receives only deliberately shaped snapshots. It retains no
-- grant on swarm.* and cannot reach work-item events, receipts, escalations or
-- other private task details.
CREATE TABLE public_api.factory_work_item_snapshot (
    work_item_id BIGINT PRIMARY KEY,
    payload JSONB NOT NULL,
    snapshotted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT ON public_api.factory_work_item_snapshot TO public_reader;
