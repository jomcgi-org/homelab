-- The worker reaper's single-writer epoch and its journal of worker pod
-- deletions. Each deletion is recorded under a stable operation ID before
-- the Kubernetes call, so a retry or a second reaper never repeats it.
CREATE TABLE reaper_writer (
    id int PRIMARY KEY CHECK (id = 1),
    epoch bigint NOT NULL,
    acquired_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE worker_deletion (
    operation_id text PRIMARY KEY,
    writer_epoch bigint NOT NULL,
    namespace text NOT NULL,
    pod text NOT NULL,
    uid text NOT NULL,
    actor_version bigint NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    outcome text,
    finished_at timestamptz
);
GRANT SELECT, INSERT, UPDATE ON reaper_writer, worker_deletion TO factory_reaper;
