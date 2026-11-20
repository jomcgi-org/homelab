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

-- A late outcome removes only the runs up to its issue's run from the streak,
-- and a replay of the same outcome changes nothing.
CREATE FUNCTION pg_temp.replay_run(run text, key text) RETURNS integer
LANGUAGE plpgsql AS $$
BEGIN
    -- now() is fixed in the test transaction: move earlier runs back in time.
    UPDATE planner_run SET started_at = now() - interval '2 hours',
        finished_at = finished_at - interval '1 minute' WHERE schedule = 'replay';
    IF NOT admit_planner_run('replay', run) THEN
        RAISE EXCEPTION 'expected to admit %', run;
    END IF;
    IF key IS NOT NULL THEN
        INSERT INTO planner_proposal(run_id, fingerprint, disposition, issue_key, closed_at)
            VALUES (run, 'homelab:docs:' || run || ':missing', 'backlog', key, now());
    END IF;
    PERFORM finish_planner_run(run);
    RETURN (SELECT low_yield FROM planner_schedule WHERE name = 'replay');
END;
$$;
DO $$
DECLARE
    streak integer;
BEGIN
    INSERT INTO planner_schedule VALUES ('replay', 'homelab:docs', 'backlog', interval '1 hour', interval '1 day', 1, 0);
    PERFORM pg_temp.replay_run('p1', NULL);
    PERFORM pg_temp.replay_run('p2', 'HOME-300');
    PERFORM pg_temp.replay_run('p3', NULL);
    streak := pg_temp.replay_run('p4', NULL);
    IF streak <> 4 THEN
        RAISE EXCEPTION 'expected streak 4 before the outcome, got %', streak;
    END IF;
    IF NOT mark_planner_reproducible('HOME-300') THEN
        RAISE EXCEPTION 'expected to mark HOME-300';
    END IF;
    streak := (SELECT low_yield FROM planner_schedule WHERE name = 'replay');
    IF streak <> 2 THEN
        RAISE EXCEPTION 'late outcome: expected the 2 later runs in the streak, got %', streak;
    END IF;
    PERFORM pg_temp.replay_run('p5', NULL);
    streak := pg_temp.replay_run('p6', NULL);
    IF streak <> 4 OR NOT mark_planner_reproducible('HOME-300') OR
        (SELECT low_yield FROM planner_schedule WHERE name = 'replay') <> 4 THEN
        RAISE EXCEPTION 'replay changed the streak from later runs';
    END IF;
    IF mark_planner_reproducible('HOME-unknown') THEN
        RAISE EXCEPTION 'unknown key marked';
    END IF;
END;
$$;

-- Each reservation limit refuses the proposal past its bound and counts it.
CREATE FUNCTION pg_temp.expect(got text, want text, why text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF got IS DISTINCT FROM want THEN
        RAISE EXCEPTION '%: got %, want %', why, got, want;
    END IF;
END;
$$;
CREATE FUNCTION pg_temp.next_run(run text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE planner_run SET started_at = now() - interval '1 hour' WHERE schedule = 'limits';
    UPDATE planner_schedule SET low_yield = 0 WHERE name = 'limits';
    IF NOT admit_planner_run('limits', run) THEN
        RAISE EXCEPTION 'expected to admit %', run;
    END IF;
END;
$$;
DO $$
BEGIN
    DELETE FROM planner_proposal;
    INSERT INTO planner_schedule VALUES ('limits', 'homelab:code', 'delegate', interval '1 minute', interval '7 days', 2, 0);
    PERFORM pg_temp.next_run('l1');
    PERFORM pg_temp.expect(reserve_planner_proposal('l1', 'homelab:code:a:x', 'agent'), 'delegate', 'first delegation');
    PERFORM pg_temp.expect(reserve_planner_proposal('l1', 'homelab:code:a:x', 'agent'), NULL, 'same run duplicate');
    PERFORM pg_temp.expect(reserve_planner_proposal('l1', 'homelab:code:b:x', 'agent'), 'delegate', 'second delegation');
    PERFORM pg_temp.expect(reserve_planner_proposal('l1', 'homelab:code:c:x', 'human'), NULL, 'per-run limit');
    PERFORM pg_temp.expect((SELECT dropped || '/' || deduplicated FROM planner_run WHERE run_id = 'l1'), '1/1', 'l1 counts');
    PERFORM finish_planner_run('l1');

    PERFORM pg_temp.next_run('l2');
    PERFORM pg_temp.expect(reserve_planner_proposal('l2', 'homelab:code:d:x', 'agent'), NULL, 'delegated limit');
    PERFORM pg_temp.expect(reserve_planner_proposal('l2', 'homelab:code:e:x', 'human'), 'joe', 'human fix under the delegated limit');
    PERFORM finish_planner_run('l2');

    PERFORM pg_temp.next_run('l3');
    PERFORM pg_temp.expect(reserve_planner_proposal('l3', 'homelab:code:f:x', 'human'), 'joe', 'fourth today');
    PERFORM pg_temp.expect(reserve_planner_proposal('l3', 'homelab:code:g:x', 'human'), NULL, 'daily limit');
    PERFORM finish_planner_run('l3');

    UPDATE planner_proposal SET reserved_at = now() - interval '48 hours';
    PERFORM pg_temp.next_run('l4');
    PERFORM pg_temp.expect(reserve_planner_proposal('l4', 'homelab:code:h:x', 'human'), 'joe', 'fifth this week');
    PERFORM pg_temp.expect(reserve_planner_proposal('l4', 'homelab:code:i:x', 'human'), 'joe', 'sixth this week');
    PERFORM finish_planner_run('l4');
    UPDATE planner_proposal SET reserved_at = now() - interval '48 hours';
    PERFORM pg_temp.next_run('l5');
    PERFORM pg_temp.expect(reserve_planner_proposal('l5', 'homelab:code:j:x', 'human'), NULL, 'weekly limit');

    UPDATE planner_proposal SET reserved_at = now() - interval '200 hours';
    PERFORM pg_temp.expect(reserve_planner_proposal('l5', 'homelab:code:a:x', 'agent'), NULL, 'open fingerprint');
    UPDATE planner_proposal SET closed_at = now() - interval '1 day' WHERE fingerprint = 'homelab:code:a:x';
    PERFORM pg_temp.expect(reserve_planner_proposal('l5', 'homelab:code:a:x', 'agent'), NULL, 'fingerprint cooldown');
    UPDATE planner_proposal SET closed_at = now() - interval '8 days' WHERE fingerprint = 'homelab:code:a:x';
    PERFORM pg_temp.expect(reserve_planner_proposal('l5', 'homelab:code:a:x', 'agent'), 'delegate', 'after the cooldown');
    PERFORM pg_temp.expect((SELECT dropped || '/' || deduplicated FROM planner_run WHERE run_id = 'l5'), '1/2', 'l5 counts');
END;
$$;
