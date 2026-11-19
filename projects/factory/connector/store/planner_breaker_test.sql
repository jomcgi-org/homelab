DO $$
DECLARE
    run text;
    streak integer;
BEGIN
    INSERT INTO planner_schedule VALUES ('smoke', 'homelab:docs', 'backlog', interval '1 hour', interval '1 day', 1, 0);
    FOR i IN 1..4 LOOP
        run := 'smoke-' || i;
        IF NOT admit_planner_run('smoke', run) THEN
            RAISE EXCEPTION 'expected to admit %', run;
        END IF;
        IF reserve_planner_proposal(run, 'homelab:docs:obj' || i || ':missing', 'agent') IS DISTINCT FROM 'backlog' THEN
            RAISE EXCEPTION 'expected a Backlog reservation for %', run;
        END IF;
        IF i % 2 = 0 THEN
            UPDATE planner_proposal SET issue_key = 'HOME-' || i, closed_at = now()
                WHERE run_id = run;
        END IF;
        IF NOT finish_planner_run(run) THEN
            RAISE EXCEPTION 'expected to finish %', run;
        END IF;
        SELECT low_yield INTO streak FROM planner_schedule WHERE name = 'smoke';
        IF streak <> i THEN
            RAISE EXCEPTION 'expected streak %, got %', i, streak;
        END IF;
        UPDATE planner_run SET started_at = now() - interval '2 hours' WHERE run_id = run;
    END LOOP;
    IF admit_planner_run('smoke', 'blocked') OR finish_planner_run('smoke-4') THEN
        RAISE EXCEPTION 'breaker or completion replay failed';
    END IF;
END;
$$;

DO $$
BEGIN
    INSERT INTO planner_schedule VALUES ('positive', 'homelab:docs', 'backlog', interval '1 hour', interval '1 day', 1, 3);
    PERFORM admit_planner_run('positive', 'useful');
    INSERT INTO planner_proposal(run_id, fingerprint, disposition, issue_key, reproducible)
        VALUES ('useful', 'homelab:docs:guide:missing', 'backlog', 'HOME-100', true);
    PERFORM finish_planner_run('useful');
    IF (SELECT low_yield FROM planner_schedule WHERE name = 'positive') <> 0 THEN
        RAISE EXCEPTION 'filed reproducible finding failed to reset the streak';
    END IF;
END;
$$;
