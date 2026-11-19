ALTER TABLE outbox ADD COLUMN unavailable_since timestamptz;
CREATE INDEX outbox_pending_outage ON outbox (unavailable_since)
WHERE unavailable_since IS NOT NULL AND delivered_at IS NULL AND abandoned_at IS NULL;
