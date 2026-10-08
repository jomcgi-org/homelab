CREATE TABLE knowledge.review_pilot_runs (
    request_id text PRIMARY KEY,
    job text NOT NULL,
    actor text NOT NULL,
    workflow_name text NOT NULL UNIQUE,
    dry_run_request_id text UNIQUE REFERENCES knowledge.review_pilot_runs(request_id),
    active_slot integer UNIQUE CHECK (active_slot = 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    result jsonb NOT NULL DEFAULT '{}'::jsonb
);
-- NULL active_slot marks an observed terminal workflow. Retain every receipt:
-- expiry of an Argo object must never make an old request eligible to submit.
