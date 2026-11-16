-- One kagent session the connector deletes (projects/factory/ARCHITECTURE.md,
-- "Finished sessions"). Writer.Tx checks the epoch and commits this record,
-- with any release of the work item's session, before each DeleteSession
-- call. The session ID is the call's idempotency key: kagent answers a delete
-- of a deleted session with NOT_FOUND. confirmed_at is set once kagent
-- answers NOT_FOUND or a complete listing by the same caller boundary
-- (caller_identity, kagent.CallerIdentity.Key) no longer shows the session,
-- so a lost response or a delete left DELETING is retried until then.
CREATE TABLE session_deletion (
    context_id text PRIMARY KEY,
    work_item_id bigint NOT NULL REFERENCES work_item (id),
    reason text NOT NULL CHECK (reason IN ('done', 'stopped', 'replaced')),
    writer_epoch bigint NOT NULL,
    caller_identity text NOT NULL,
    requests integer NOT NULL DEFAULT 1,
    requested_at timestamptz NOT NULL DEFAULT now(),
    last_requested_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz
);
