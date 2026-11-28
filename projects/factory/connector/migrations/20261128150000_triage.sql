ALTER TABLE planner_run ADD COLUMN triage_snapshot jsonb;
CREATE TABLE triage_action (
    id bigserial PRIMARY KEY,
    run_id text NOT NULL REFERENCES planner_run(run_id),
    op text NOT NULL,
    target text NOT NULL,
    args jsonb NOT NULL,
    reason text NOT NULL,
    state_before jsonb NOT NULL,
    result text NOT NULL CHECK (result = 'observed')
);

CREATE TABLE triage_read (
    message_id text NOT NULL,
    operation text NOT NULL,
    target text NOT NULL,
    writer_epoch bigint NOT NULL,
    attempts integer NOT NULL DEFAULT 1,
    refusals integer NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, operation, target)
);

-- Observing a quiet board is a successful run even when it yields no proposal.
CREATE OR REPLACE FUNCTION finish_planner_run(incoming text) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
    job text;
BEGIN
    SELECT schedule INTO job FROM planner_run WHERE run_id = incoming;
    PERFORM 1 FROM planner_schedule WHERE name = job FOR UPDATE;
    UPDATE planner_run SET finished_at = now()
        WHERE run_id = incoming AND finished_at IS NULL;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    UPDATE planner_schedule SET low_yield =
        CASE WHEN EXISTS (
            SELECT FROM planner_run WHERE run_id = incoming AND triage_snapshot IS NOT NULL
                AND report_status = 'success' AND (dropped = 0 OR EXISTS (
                    SELECT FROM triage_action WHERE run_id = incoming))
        ) THEN 0
        WHEN EXISTS (SELECT FROM planner_proposal WHERE run_id = incoming
            AND reproducible AND closed_at IS NULL) THEN 0 ELSE least(4, low_yield + 1) END
        WHERE name = job;
    RETURN true;
END;
$$;
