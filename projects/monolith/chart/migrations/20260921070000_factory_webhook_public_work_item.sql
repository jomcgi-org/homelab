-- Durable webhook replay fencing. The row is inserted and committed in the
-- same transaction as the work-item mutation, so a failed handler leaves no
-- fence that could suppress GitHub's legitimate retry.
CREATE TABLE swarm.factory_webhook_delivery (
    delivery_id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    action TEXT,
    repo TEXT NOT NULL,
    issue_number INTEGER,
    source_updated_at TIMESTAMPTZ,
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
            'trusted_stale_ignored',
            'trusted_missing_timestamp_ignored',
            'local_untouched',
            'semi_trusted_held',
            'untrusted_ignored'
        )
    ),
    work_item_id BIGINT REFERENCES swarm.work_item (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX factory_webhook_delivery_issue_source_idx
    ON swarm.factory_webhook_delivery (repo, issue_number, source_updated_at);

-- This source watermark is independent of work-item existence. In particular,
-- a close that arrives before import prevents an older open delivery or sweep
-- snapshot from minting a phantom item. Only GitHub issue.updated_at advances
-- the watermark; delivery arrival time is never used for source ordering.
CREATE TABLE swarm.factory_github_issue_state (
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL,
    source_state TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, issue_number),
    CONSTRAINT factory_github_issue_state_source_state_check CHECK (
        source_state IN ('open', 'closed')
    ),
    CONSTRAINT factory_github_issue_state_issue_number_check CHECK (
        issue_number > 0
    )
);

-- The public tier receives only deliberately shaped snapshots. It retains no
-- grant on swarm.* and cannot reach work-item events, receipts, escalations or
-- other private task details.
CREATE TABLE public_api.factory_work_item_snapshot (
    work_item_id BIGINT PRIMARY KEY,
    payload JSONB NOT NULL,
    snapshotted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT ON public_api.factory_work_item_snapshot TO public_reader;
