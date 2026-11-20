CREATE TABLE run_phase_send (
    run_id bigint PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    writer_epoch bigint NOT NULL,
    sequence bigint NOT NULL,
    attempted_at timestamptz NOT NULL
);
