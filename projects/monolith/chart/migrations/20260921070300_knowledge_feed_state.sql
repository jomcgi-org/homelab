-- A feed with no explicit since override starts at its first enabled pass and
-- reuses that floor across pod restarts. The primary key makes concurrent
-- first passes converge without resetting the original timestamp.
CREATE TABLE knowledge.feed_state (
    feed_name        TEXT PRIMARY KEY,
    first_enabled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
