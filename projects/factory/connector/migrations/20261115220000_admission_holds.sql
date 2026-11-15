-- Admission holds consume no delivery attempt or agent concurrency slot.
ALTER TABLE run ADD COLUMN hold_reason text NOT NULL DEFAULT '';
ALTER TABLE run ADD COLUMN hold_since timestamptz;

-- Journal reads before calling itsaplan. Replays use the same identity but
-- always fetch current evidence; a previous completed read grants no admission.
CREATE TABLE admission_read (
    message_id text PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES run (id),
    run_attempt integer NOT NULL,
    operation text NOT NULL,
    resource text NOT NULL,
    writer_epoch bigint NOT NULL,
    attempts integer NOT NULL DEFAULT 1,
    attempted_at timestamptz NOT NULL DEFAULT now()
);
