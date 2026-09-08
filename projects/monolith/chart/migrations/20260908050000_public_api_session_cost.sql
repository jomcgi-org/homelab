-- Issue #5927 backs the goal summary this view feeds.
CREATE VIEW public_api.agent_session_cost_7d AS
SELECT MAX(session_cost_usd) AS max_session_cost_usd
FROM (
  SELECT s.id,
         SUM(COALESCE(t.cost_usd, 0) + COALESCE(t.list_cost_usd, 0)) AS session_cost_usd
  FROM agent_sessions.agent_turns t
  JOIN agent_sessions.agent_sessions s ON s.id = t.session_id
  WHERE t.created_at > now() - interval '7 days'
  GROUP BY s.id
) per_session;

GRANT SELECT ON public_api.agent_session_cost_7d TO public_reader;
