-- The head a review round was asked to change. A round that closes with the
-- PR still at this head pushed nothing, so no new check or review can start
-- another round; the connector reports it once on the issue.
ALTER TABLE run ADD COLUMN round_head_sha text;

-- Set before an at-most-once comment is sent. itsaplan comments carry no
-- idempotency key, so a send whose outcome is unknown is never repeated.
ALTER TABLE outbox ADD COLUMN send_started_at timestamptz;
