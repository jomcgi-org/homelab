-- Issue #5897 exposes aggregate agent activity without session identities or content.

CREATE VIEW public_api.agent_activity_daily AS
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
  SUM(t.list_cost_usd) AS list_cost_usd
FROM agent_sessions.agent_turns t
JOIN agent_sessions.agent_sessions s ON s.id = t.session_id
WHERE t.created_at > now() - interval '90 days'
  AND t.usage_json LIKE '{%'  -- Guard against empty strings or non-JSON
GROUP BY 1, 2;

GRANT SELECT ON public_api.agent_activity_daily TO public_reader;

CREATE VIEW public_api.agent_activity_now AS
SELECT
  (SELECT COUNT(*) FROM agent_sessions.agent_sessions WHERE last_turn_at > now() - interval '1 hour') AS active_last_hour,
  (SELECT COUNT(*) FROM agent_sessions.agent_sessions WHERE created_at >= date_trunc('day', now())) AS sessions_today,
  (SELECT MAX(last_turn_at) FROM agent_sessions.agent_sessions) AS last_turn_at,
  (SELECT COUNT(*) FROM agent_sessions.agent_sessions WHERE status = 'running') AS running;

GRANT SELECT ON public_api.agent_activity_now TO public_reader;
