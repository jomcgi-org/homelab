-- Freeze event boundaries before posting them. A task snapshot may grow while
-- an acknowledgement is lost, so offsets cannot be reconstructed from it.
CREATE TABLE task_transcript (
    run_id bigint NOT NULL REFERENCES run (id),
    attempt integer NOT NULL CHECK (attempt >= 1),
    task_id text NOT NULL,
    base_offset integer NOT NULL CHECK (base_offset >= 0),
    state jsonb NOT NULL,
    PRIMARY KEY (run_id, attempt, task_id)
);
