ALTER TABLE work_item ADD COLUMN pr_create_check_at timestamptz;

CREATE TABLE stopped_pr_attempt (
    message_id text PRIMARY KEY,
    attempts integer NOT NULL DEFAULT 1,
    review_id bigint
);
