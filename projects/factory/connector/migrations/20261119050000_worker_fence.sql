-- Keep the assignment while the actor clears it on crash. A later turn in
-- the same session must independently establish its own worker assignment.
CREATE TABLE worker_fence (
    attempt_id bigint PRIMARY KEY REFERENCES delivery_attempt(id),
    session_id text NOT NULL,
    namespace text NOT NULL,
    pod text NOT NULL,
    uid text NOT NULL,
    gone_at timestamptz
);
