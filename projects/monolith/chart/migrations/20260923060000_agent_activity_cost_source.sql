-- Append provenance without changing the existing day/model grain or cost sums.
-- Reported and list-price amounts remain separate. NULL means neither is known;
-- mixed means both columns contribute to this aggregate, including zero costs.
CREATE OR REPLACE VIEW public_api.agent_activity_daily AS
SELECT
  date_trunc('day', t.created_at)::date AS day,
  COALESCE(s.model, 'unknown') AS model,
  COUNT(DISTINCT s.id) AS sessions,
  COUNT(t.id) AS turns,
  SUM(NULLIF(t.usage_json::jsonb->>'input_tokens', '')::numeric::bigint) AS input_tokens,
  SUM(NULLIF(t.usage_json::jsonb->>'output_tokens', '')::numeric::bigint) AS output_tokens,
  SUM(COALESCE(
    NULLIF(t.usage_json::jsonb->>'cache_read_tokens', '')::numeric::bigint,
    NULLIF(t.usage_json::jsonb->>'cache_read_input_tokens', '')::numeric::bigint,
    NULLIF(t.usage_json::jsonb->>'cached_input_tokens', '')::numeric::bigint,
    0
  )) AS cache_read_tokens,
  SUM(COALESCE(
    NULLIF(t.usage_json::jsonb->>'cache_write_tokens', '')::numeric::bigint,
    NULLIF(t.usage_json::jsonb->>'cache_creation_input_tokens', '')::numeric::bigint,
    NULLIF(t.usage_json::jsonb->>'cache_write_input_tokens', '')::numeric::bigint,
    0
  )) AS cache_write_tokens,
  SUM(t.cost_usd) AS cost_usd,
  SUM(t.list_cost_usd) AS list_cost_usd,
  CASE
    WHEN BOOL_OR(t.cost_usd IS NOT NULL) AND BOOL_OR(t.list_cost_usd IS NOT NULL) THEN 'mixed'
    WHEN BOOL_OR(t.cost_usd IS NOT NULL) THEN 'reported'
    WHEN BOOL_OR(t.list_cost_usd IS NOT NULL) THEN 'list'
    ELSE NULL
  END AS cost_source
FROM agent_sessions.agent_turns t
JOIN agent_sessions.agent_sessions s ON s.id = t.session_id
WHERE t.created_at > now() - interval '90 days'
  AND t.usage_json LIKE '{%'
  AND t.usage_json !~ '(^|[^\\])(\\\\)*\\u0000'
GROUP BY 1, 2;

GRANT SELECT ON public_api.agent_activity_daily TO public_reader;
GRANT SELECT ON public_api.agent_activity_now TO public_reader;
