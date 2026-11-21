-- Only schedules loaded from reviewed config admit a run. Disabled rows
-- keep the history referenced by existing runs.
ALTER TABLE planner_schedule ADD COLUMN enabled boolean NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION admit_planner_run(job text, incoming text) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
    settings planner_schedule%ROWTYPE;
BEGIN
    SELECT * INTO settings FROM planner_schedule WHERE name = job FOR UPDATE;
    IF NOT FOUND OR NOT settings.enabled OR settings.low_yield >= 4 OR EXISTS (
        SELECT FROM planner_run WHERE schedule = job AND
            (finished_at IS NULL OR started_at > now() - settings.min_interval)
    ) THEN
        RETURN false;
    END IF;
    INSERT INTO planner_run(run_id, schedule) VALUES (incoming, job)
        ON CONFLICT DO NOTHING;
    RETURN FOUND;
END;
$$;
