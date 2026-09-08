-- Issue #5918 folds aggregate local Mac sessions into the public activity surface.

CREATE VIEW public_api.local_session_activity_daily AS
SELECT
  (extra->>'started_at')::timestamptz::date AS day,
  COALESCE(extra->>'model', 'unknown') AS model,
  source,
  COUNT(*) AS sessions,
  SUM(NULLIF((extra->>'usage')::jsonb->>'input_tokens', '')::numeric::bigint) AS input_tokens,
  SUM(NULLIF((extra->>'usage')::jsonb->>'output_tokens', '')::numeric::bigint) AS output_tokens,
  SUM(NULLIF((extra->>'usage')::jsonb->>'cache_read_tokens', '')::numeric::bigint) AS cache_read_tokens,
  SUM((extra->>'usage_cost_usd')::numeric) AS list_cost_usd
FROM knowledge.raw_inputs
WHERE source IN ('claude-session', 'codex-session')
  AND extra ? 'usage'
  AND created_at > now() - interval '90 days'
GROUP BY day, model, source;

GRANT SELECT ON public_api.local_session_activity_daily TO public_reader;
