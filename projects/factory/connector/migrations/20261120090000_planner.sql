-- A schedule can have only one active analysis. Replays of an admitted run
-- reuse its durable result instead of spending another slot.
CREATE TABLE planner_schedule (
    name text PRIMARY KEY,
    namespace text NOT NULL,
    ceiling text NOT NULL CHECK (ceiling IN ('delegate', 'joe', 'backlog')),
    min_interval interval NOT NULL CHECK (min_interval > interval '0'),
    cooldown interval NOT NULL CHECK (cooldown > interval '0'),
    per_run integer NOT NULL CHECK (per_run BETWEEN 1 AND 4),
    low_yield integer NOT NULL DEFAULT 0 CHECK (low_yield BETWEEN 0 AND 4)
);

CREATE TABLE planner_run (
    run_id text PRIMARY KEY,
    schedule text NOT NULL REFERENCES planner_schedule(name),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    input_tokens bigint CHECK (input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens >= 0),
    dropped integer NOT NULL DEFAULT 0 CHECK (dropped >= 0),
    deduplicated integer NOT NULL DEFAULT 0 CHECK (deduplicated >= 0)
);
CREATE UNIQUE INDEX planner_run_one_active ON planner_run(schedule) WHERE finished_at IS NULL;

CREATE FUNCTION admit_planner_run(job text, incoming text) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
    settings planner_schedule%ROWTYPE;
BEGIN
    SELECT * INTO settings FROM planner_schedule WHERE name = job FOR UPDATE;
    IF NOT FOUND OR settings.low_yield >= 4 OR EXISTS (
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

-- Reservations count towards budgets while the tracker write is pending.
CREATE TABLE planner_proposal (
    run_id text NOT NULL REFERENCES planner_run(run_id),
    fingerprint text NOT NULL,
    disposition text NOT NULL CHECK (disposition IN ('delegate', 'joe', 'backlog')),
    reserved_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    issue_key text UNIQUE,
    reproducible boolean NOT NULL DEFAULT false CHECK (NOT reproducible OR issue_key IS NOT NULL),
    PRIMARY KEY (run_id, fingerprint)
);
CREATE UNIQUE INDEX planner_open_fingerprint ON planner_proposal(fingerprint) WHERE closed_at IS NULL;
CREATE INDEX planner_portfolio_window ON planner_proposal(reserved_at);

CREATE FUNCTION reserve_planner_proposal(incoming text, fp text, fix text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    settings planner_schedule%ROWTYPE;
    route text;
    daily integer;
    weekly integer;
    delegated integer;
BEGIN
    -- Serialize portfolio decisions across jobs before reading the totals.
    PERFORM pg_advisory_xact_lock(7935, 40);
    SELECT s.* INTO settings FROM planner_schedule s JOIN planner_run r
        ON r.schedule = s.name WHERE r.run_id = incoming AND r.finished_at IS NULL
        FOR UPDATE OF s, r;
    IF NOT FOUND OR fp IS NULL OR length(fp) > 256 OR
        fp !~ '^[a-z0-9._/-]+(:[a-z0-9._/-]+){3}$' OR
        split_part(fp, ':', 1) || ':' || split_part(fp, ':', 2) <> settings.namespace OR
        fix IS NULL OR fix NOT IN ('human', 'agent') THEN
        RETURN NULL;
    END IF;
    IF EXISTS (SELECT FROM planner_proposal WHERE fingerprint = fp AND
            (closed_at IS NULL OR closed_at > now() - settings.cooldown OR run_id = incoming)) THEN
        UPDATE planner_run SET deduplicated = deduplicated + 1 WHERE run_id = incoming;
        RETURN NULL;
    END IF;
    route := CASE WHEN settings.ceiling = 'delegate' AND fix = 'human'
        THEN 'joe' ELSE settings.ceiling END;
    SELECT count(*) FILTER (WHERE reserved_at > now() - interval '24 hours'),
           count(*), count(*) FILTER (WHERE disposition = 'delegate'
               AND reserved_at > now() - interval '24 hours')
        INTO daily, weekly, delegated FROM planner_proposal
        WHERE reserved_at > now() - interval '168 hours';
    IF daily >= 4 OR weekly >= 6 OR (route = 'delegate' AND delegated >= 2)
        OR (SELECT count(*) FROM planner_proposal WHERE run_id = incoming) >= settings.per_run THEN
        UPDATE planner_run SET dropped = dropped + 1 WHERE run_id = incoming;
        RETURN NULL;
    END IF;
    INSERT INTO planner_proposal(run_id, fingerprint, disposition) VALUES (incoming, fp, route);
    RETURN route;
END;
$$;

-- Lock in the same order as admission. Only the first completion affects the
-- streak, including when the caller retries after losing its response.
CREATE FUNCTION finish_planner_run(incoming text) RETURNS boolean
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
        CASE WHEN EXISTS (SELECT FROM planner_proposal WHERE run_id = incoming
            AND reproducible AND closed_at IS NULL) THEN 0 ELSE least(4, low_yield + 1) END
        WHERE name = job;
    RETURN true;
END;
$$;

-- Populated from the reviewed schedule configuration and isolated run result.
ALTER TABLE planner_schedule ADD COLUMN issue_title text, ADD COLUMN instructions text;
ALTER TABLE planner_run ADD COLUMN final_message text, ADD COLUMN processed boolean NOT NULL DEFAULT false;
ALTER TABLE planner_proposal ADD COLUMN title text, ADD COLUMN description text,
    ADD COLUMN priority text, ADD COLUMN create_started boolean NOT NULL DEFAULT false,
    ADD COLUMN next_attempt timestamptz NOT NULL DEFAULT now();

-- A receipt lookup abandoned after persistent refusal keeps its fingerprint
-- open: the create may already have committed in the tracker.
ALTER TABLE planner_proposal ADD COLUMN filing_stopped boolean NOT NULL DEFAULT false;
CREATE TABLE planner_tracker_attempt (
    run_id text NOT NULL,
    fingerprint text NOT NULL,
    operation text NOT NULL,
    resource text NOT NULL,
    writer_epoch bigint NOT NULL,
    attempts integer NOT NULL DEFAULT 1,
    last_attempt timestamptz NOT NULL DEFAULT now(),
    refusals integer NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, fingerprint, operation, resource),
    FOREIGN KEY (run_id, fingerprint) REFERENCES planner_proposal
);

-- A filed issue proves its finding only later, when the issue reaches Done.
-- The outcome sync (HOME-548) calls this with the filed issue key. The first
-- call marks the proposal reproducible and removes from the low-yield streak
-- the runs up to the issue's run, also after the run finished. Runs that
-- finished later stay in the streak, and a replay changes nothing. It returns
-- false for an unknown or unfiled key.
CREATE FUNCTION mark_planner_reproducible(key text) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
    job text;
    done timestamptz;
BEGIN
    SELECT r.schedule, r.finished_at INTO job, done FROM planner_proposal p JOIN planner_run r USING (run_id)
        WHERE p.issue_key = key;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM planner_schedule WHERE name = job FOR UPDATE;
    UPDATE planner_proposal SET reproducible = true WHERE issue_key = key AND NOT reproducible;
    IF FOUND THEN
        UPDATE planner_schedule SET low_yield = least(low_yield, (SELECT count(*) FROM planner_run
            WHERE schedule = job AND finished_at > done)) WHERE name = job;
    END IF;
    RETURN true;
END;
$$;
