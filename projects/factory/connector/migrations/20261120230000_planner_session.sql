ALTER TABLE planner_run ADD COLUMN caller_identity text, ADD COLUMN session_id text, ADD COLUMN task_id text;
ALTER TABLE planner_run
    ADD COLUMN cancel_request_id text,
    ADD COLUMN cancel_writer_epoch bigint,
    ADD COLUMN cancel_attempts bigint NOT NULL DEFAULT 0;
