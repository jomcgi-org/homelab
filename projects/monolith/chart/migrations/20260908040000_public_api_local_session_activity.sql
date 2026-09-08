-- Issue #5918 folds aggregate local Mac sessions into the public activity surface.

CREATE VIEW public_api.local_session_activity_daily AS
SELECT
  created_at::date AS day,
  COALESCE(extra->>'model', 'unknown') AS model,
  source,
  COUNT(*) AS sessions,
  SUM(NULLIF(regexp_replace(extra->'usage'->>'input_tokens', '[^0-9]', '', 'g'), '')::bigint) AS input_tokens,
  SUM(NULLIF(regexp_replace(extra->'usage'->>'output_tokens', '[^0-9]', '', 'g'), '')::bigint) AS output_tokens,
  SUM(NULLIF(regexp_replace(extra->'usage'->>'cache_read_tokens', '[^0-9]', '', 'g'), '')::bigint) AS cache_read_tokens,
  SUM(
    CASE
      WHEN extra->>'usage_cost_usd' ~ '^[0-9.]+$'
        AND extra->>'usage_cost_usd' ~ '^[0-9]+([.][0-9]+)?$'
      THEN (extra->>'usage_cost_usd')::numeric
      ELSE NULL
    END
  ) AS list_cost_usd
FROM knowledge.raw_inputs
WHERE source IN ('claude-session', 'codex-session')
  AND extra ? 'usage'
  AND jsonb_typeof(extra->'usage') = 'object'
  AND created_at > now() - interval '90 days'
GROUP BY day, model, source;

GRANT SELECT ON public_api.local_session_activity_daily TO public_reader;
