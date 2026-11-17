-- CloudNativePG provisions this login before the connector rolls out. The
-- connector owns the functions; the broker has no table or sequence grants.
-- Qualified objects and a fixed search path prevent caller-created objects
-- from running with the connector's privileges.
CREATE FUNCTION public.record_codex_call(text, text, text) RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    INSERT INTO public.codex_call (atespace, session_id, actor_uid, delivery_attempt_id)
    VALUES ($1, $2, $3, (
        SELECT id FROM public.delivery_attempt
        WHERE kagent_namespace = $1 AND lower(context_id) = $2 AND issues > 0
        ORDER BY id DESC LIMIT 1)) RETURNING id
$$;

CREATE FUNCTION public.record_codex_usage(bigint, text, bigint, bigint, bigint) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    INSERT INTO public.codex_usage (call_id, response_id, input_tokens, cached_input_tokens, output_tokens)
    VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING
$$;

REVOKE ALL ON FUNCTION public.record_codex_call(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.record_codex_usage(bigint, text, bigint, bigint, bigint) FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO factory_broker_accounting;
GRANT EXECUTE ON FUNCTION public.record_codex_call(text, text, text) TO factory_broker_accounting;
GRANT EXECUTE ON FUNCTION public.record_codex_usage(bigint, text, bigint, bigint, bigint) TO factory_broker_accounting;
