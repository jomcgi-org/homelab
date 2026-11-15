-- A stable message identifies each ejection read across retries and writer
-- takeovers. Queries are idempotent and always fetch fresh state; attempts are
-- recorded before the call without caching a permission or hold decision.
CREATE TABLE delivery_read (
    message_id text PRIMARY KEY,
    pull_request_id bigint NOT NULL REFERENCES pull_request (id),
    operation text NOT NULL,
    resource text NOT NULL,
    writer_epoch bigint NOT NULL,
    attempts integer NOT NULL DEFAULT 1,
    attempted_at timestamptz NOT NULL DEFAULT now()
);
