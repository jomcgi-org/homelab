-- HOME-245: unknown historical inputs remain NULL. Dispatch snapshots inputs
-- before each send. Once execution is possible, the run snapshot is immutable.
ALTER TABLE run
    ADD COLUMN provenance_required boolean NOT NULL DEFAULT false,
    ADD COLUMN guidance_system_prompt text,
    ADD COLUMN model text,
    ADD COLUMN reasoning_effort text,
    ADD COLUMN arm text,
    ADD COLUMN base_sha text,
    ADD COLUMN prompt_hash text,
    ADD COLUMN task_class text,
    ADD COLUMN reviewer_message_id text UNIQUE;
ALTER TABLE run ALTER COLUMN provenance_required SET DEFAULT true;
ALTER TABLE delivery_attempt
    ADD COLUMN rendered_prompt text,
    ADD COLUMN guidance_system_prompt text,
    ADD COLUMN model text,
    ADD COLUMN reasoning_effort text,
    ADD COLUMN arm text,
    ADD COLUMN base_sha text,
    ADD COLUMN prompt_hash text,
    ADD COLUMN task_class text;
ALTER TABLE delivery_attempt DROP CONSTRAINT delivery_attempt_error_class_check;
ALTER TABLE delivery_attempt ADD CONSTRAINT delivery_attempt_error_class_check
    CHECK (error_class IN ('engineering', 'credential', 'quota', 'canceled', 'abandoned', 'bounded_output', 'runtime_changed'));
-- Historical sends might have executed. New attempts become possible before
-- Send, and only a definitive refusal can clear that witness.
ALTER TABLE delivery_attempt ADD COLUMN execution_possible boolean NOT NULL DEFAULT true;
ALTER TABLE delivery_attempt ALTER COLUMN execution_possible SET DEFAULT false;

ALTER TABLE run DROP CONSTRAINT run_origin_check;
ALTER TABLE run ADD CONSTRAINT run_origin_check
    CHECK (origin IN ('itsaplan', 'connector', 'reviewer'));

-- Reviewer execution is owned by the reviewer service. These rows are closed
-- to connector dispatch, not a claim that the review finished or passed.
-- The broker has function-only access, as for token accounting. It cannot
-- modify coder runs, delivery state, or a previously recorded snapshot.
CREATE FUNCTION public.record_reviewer_run(text, text, text, text, text, text, text, text, text)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
DECLARE recorded public.run; result bigint;
BEGIN
    IF $1 = '' OR $2 = '' OR $4 = '' OR $5 = '' OR $6 = '' OR $9 = ''
        OR $7 !~ '^[0-9a-f]{40}$' OR $8 !~ '^[0-9a-f]{64}$'
        OR $1 IS NULL OR $2 IS NULL OR $4 IS NULL OR $5 IS NULL
        OR $6 IS NULL OR $7 IS NULL OR $8 IS NULL OR $9 IS NULL THEN
        RAISE EXCEPTION 'incomplete reviewer provenance';
    END IF;
    INSERT INTO public.run (id, origin, agent_key, issue_key, trigger, prompt, system_prompt,
        attempt, state, closed_reason, reviewer_message_id,
        model, reasoning_effort, arm, base_sha, prompt_hash, task_class)
    VALUES (nextval('public.connector_run_id'), 'reviewer', $2, nullif($3, ''), 'reviewer', '', '',
        1, 'closed', 'tracked_externally', $1, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (reviewer_message_id) DO NOTHING RETURNING id INTO result;
    IF result IS NOT NULL THEN RETURN result; END IF;
    SELECT * INTO STRICT recorded FROM public.run WHERE reviewer_message_id = $1;
    IF (recorded.agent_key, coalesce(recorded.issue_key, ''), recorded.model,
        recorded.reasoning_effort, recorded.arm, recorded.base_sha, recorded.prompt_hash, recorded.task_class)
        IS DISTINCT FROM ($2, coalesce($3, ''), $4, $5, $6, $7, $8, $9) THEN
        RAISE EXCEPTION 'reviewer provenance conflicts with recorded message';
    END IF;
    RETURN recorded.id;
END
$$;
REVOKE ALL ON FUNCTION public.record_reviewer_run(text, text, text, text, text, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.record_reviewer_run(text, text, text, text, text, text, text, text, text) TO factory_broker_accounting;
