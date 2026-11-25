-- HOME-727. Bind the accepted native task to its immutable review receipt.
ALTER TABLE public.run
    ADD COLUMN reviewer_context_id text,
    ADD COLUMN reviewer_task_id text,
    ADD CHECK ((reviewer_context_id IS NULL) = (reviewer_task_id IS NULL));

CREATE UNIQUE INDEX reviewer_native_task
    ON public.run (agent_key, reviewer_context_id, reviewer_task_id)
    WHERE origin = 'reviewer' AND reviewer_task_id IS NOT NULL;

CREATE FUNCTION public.bind_reviewer_task(text, text, text, text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF $3 IS NULL OR $4 IS NULL OR $3 !~ '^[a-z0-9][a-z0-9-]{0,127}$'
        OR $4 !~ '^[a-z0-9][a-z0-9-]{0,127}$' THEN
        RAISE EXCEPTION 'invalid reviewer task identity';
    END IF;

    UPDATE public.run
    SET reviewer_context_id = $3, reviewer_task_id = $4
    WHERE origin = 'reviewer' AND reviewer_message_id = $1 AND agent_key = $2
        AND (reviewer_task_id IS NULL OR
             (reviewer_context_id = $3 AND reviewer_task_id = $4));
    IF NOT FOUND THEN
        RAISE EXCEPTION 'acceptance does not match a recorded review';
    END IF;
END
$$;

CREATE FUNCTION public.reviewer_task_main(text, text, text)
RETURNS SETOF text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
    SELECT base_sha FROM public.run
    WHERE origin = 'reviewer' AND agent_key = $1
        AND reviewer_context_id = $2 AND reviewer_task_id = $3
        AND reviewer_task_id IS NOT NULL AND base_sha IS NOT NULL
$$;

REVOKE ALL ON FUNCTION public.bind_reviewer_task(text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.reviewer_task_main(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.bind_reviewer_task(text, text, text, text) TO factory_broker_accounting;
GRANT EXECUTE ON FUNCTION public.reviewer_task_main(text, text, text) TO factory_broker_accounting;
