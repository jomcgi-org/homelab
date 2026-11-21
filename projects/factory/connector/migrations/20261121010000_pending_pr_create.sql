ALTER TABLE work_item ADD COLUMN pr_create_check_at timestamptz,
    ADD COLUMN pr_create_started_at timestamptz,
    ADD COLUMN pr_create_absence_confirmed boolean NOT NULL DEFAULT false;

CREATE TABLE stopped_pr_attempt (
    message_id text PRIMARY KEY,
    attempts integer NOT NULL DEFAULT 1,
    confirmed boolean NOT NULL DEFAULT false
);
