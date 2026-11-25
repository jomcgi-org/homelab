/* invariant: HOME-40-1 */
-- Planner runtime (HOME-546, HOME-548, HOME-549). An itsaplan cron run of the
-- planner agent becomes a planner_run through admit_planner_run, and the
-- connector sends its session one message. The columns record each step
-- before it happens, so a replayed claim or a lost response never starts a
-- second session.
ALTER TABLE planner_run
    ADD COLUMN itsaplan_run_id bigint UNIQUE,
    ADD COLUMN itsaplan_attempt integer,
    ADD COLUMN trigger text,
    ADD COLUMN message_id text,
    ADD COLUMN context_id text,
    ADD COLUMN task_id text,
    ADD COLUMN send_started_at timestamptz,
    ADD COLUMN send_attempts integer NOT NULL DEFAULT 0 CHECK (send_attempts >= 0),
    ADD COLUMN heartbeat_at timestamptz,
    ADD COLUMN failure text,
    ADD COLUMN report_status text CHECK (report_status IN ('success', 'failed')),
    ADD COLUMN reported_at timestamptz,
    ADD COLUMN report_attempts integer NOT NULL DEFAULT 0 CHECK (report_attempts >= 0),
    ADD COLUMN report_after timestamptz,
    ADD COLUMN session_deleted_at timestamptz,
    ADD COLUMN delete_attempts integer NOT NULL DEFAULT 0 CHECK (delete_attempts >= 0);

-- The analysis prompt comes from the reviewed schedule. A schedule with none
-- admits no run.
ALTER TABLE planner_schedule ADD COLUMN prompt text NOT NULL DEFAULT '';

-- A filed issue keeps its id for outcome reads. outcome_checked_at spaces the
-- reads; a Done issue makes its proposal reproducible and a Canceled one
-- leaves it rejected (closed, not reproducible).
ALTER TABLE planner_proposal
    ADD COLUMN issue_id bigint,
    ADD COLUMN outcome_checked_at timestamptz;

-- The usage the broker measured for a planner session, summed once the task
-- ends. NULL stays unmeasured, never zero.
CREATE FUNCTION planner_session_usage(space text, session text,
    OUT input_tokens bigint, OUT output_tokens bigint)
LANGUAGE sql STABLE AS $$
    SELECT sum(u.input_tokens)::bigint, sum(u.output_tokens)::bigint
    FROM codex_usage u JOIN codex_call c ON c.id = u.call_id
    WHERE c.atespace = space AND c.session_id = lower(session)
$$;
