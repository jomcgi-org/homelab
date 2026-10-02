-- Read-only factory outcomes report (#6716): what each (role, model) costs in
-- money and wall time per positive outcome. See outcomes.md for definitions.
--
-- psql "$DATABASE_URL" -X -v window_start='2026-09-07T00:00:00Z' \
--   -v window_end='2026-10-03T00:00:00Z' -f outcomes_report.sql
--
-- Cost is reported as a bracket. list_usd sums the attempt's own priced turns
-- (agent_turns.list_cost_usd through swarm_node_run.session_id): a lower bound,
-- because error turns record no usage and so no price. reserved_usd is the
-- factory_start ledger, where an unsettled start counts its max_cost_usd cap: an
-- upper bound. unpriced_turns says how wide the gap is.

SET default_transaction_read_only = on;

\echo '== Attempts by role and model'
WITH turn_cost AS (
    SELECT t.session_id, SUM(t.list_cost_usd) AS cost,
           COUNT(*) FILTER (WHERE t.list_cost_usd IS NULL) AS unpriced
    FROM agent_sessions.agent_turns t
    GROUP BY t.session_id
),
runs AS (
    SELECT
        CASE WHEN r.node_key LIKE 'conductor_funding%' THEN 'funding'
             ELSE split_part(r.node_key, '_', 1) END AS role,
        COALESCE(r.model, r.pin_json::jsonb ->> 'model') AS model,
        r.status,
        tc.cost,
        tc.unpriced,
        COALESCE(s.cost_usd, s.max_cost_usd) AS reserved,
        EXTRACT(EPOCH FROM r.finished_at - r.created_at) / 60 AS minutes
    FROM swarm.swarm_node_run r
    LEFT JOIN turn_cost tc ON tc.session_id = r.session_id
    LEFT JOIN swarm.factory_start s ON s.start_key = r.dispatch_key
    WHERE r.status IN ('succeeded', 'failed', 'escalated', 'cancelled')
      AND r.created_at >= :'window_start' AND r.created_at < :'window_end'
)
SELECT role, model, COUNT(*) AS attempts,
       ROUND(100.0 * AVG((status = 'succeeded')::int)) AS ok_pct,
       COALESCE(SUM(unpriced), 0) AS unpriced_turns,
       ROUND(SUM(cost)::numeric, 2) AS list_usd,
       ROUND(SUM(reserved)::numeric, 2) AS reserved_usd,
       ROUND((SUM(cost) / NULLIF(SUM((status = 'succeeded')::int), 0))::numeric, 3)
           AS usd_per_ok,
       ROUND((SUM(minutes) / NULLIF(SUM((status = 'succeeded')::int), 0))::numeric, 1)
           AS minutes_per_ok,
       ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY minutes))::numeric, 1)
           AS p50_minutes
FROM runs
GROUP BY role, model
ORDER BY role, list_usd DESC NULLS LAST;

\echo '== Tasks by implementing model and size band'
WITH turn_cost AS (
    SELECT split_part(s.local_session_id, ':', 2) AS task_id,
           SUM(t.list_cost_usd) AS cost
    FROM agent_sessions.agent_sessions s
    JOIN agent_sessions.agent_turns t ON t.session_id = s.id
    WHERE s.local_session_id LIKE 'factory:%'
    GROUP BY 1
),
merged AS (
    SELECT DISTINCT ON (a.task_id) a.task_id, a.created_at AS merged_at,
           (a.detail_json::jsonb ->> 'pr_number')::int AS pr_number,
           a.detail_json::jsonb -> 'ci' ->> 'conclusion' AS ci_conclusion
    FROM swarm.factory_audit a
    WHERE a.action = 'merged'
    ORDER BY a.task_id, a.created_at
),
reverted AS (
    SELECT DISTINCT task_id FROM swarm.factory_audit WHERE action = 'reverted'
),
impl AS (
    SELECT r.task_id,
           MODE() WITHIN GROUP (ORDER BY COALESCE(r.model, r.pin_json::jsonb ->> 'model'))
               AS model
    FROM swarm.swarm_node_run r
    WHERE r.node_key LIKE 'implement%'
    GROUP BY r.task_id
),
tasks AS (
    SELECT fr.task_id, fr.created_at, impl.model, tc.cost, m.merged_at,
           m.ci_conclusion, (rv.task_id IS NOT NULL) AS reverted,
           CASE WHEN p.number IS NULL THEN 'unknown'
                WHEN p.additions + p.deletions < 50 THEN 'S'
                WHEN p.additions + p.deletions < 300 THEN 'M'
                ELSE 'L' END AS size_band
    FROM swarm.factory_receipt fr
    LEFT JOIN impl ON impl.task_id = fr.task_id
    LEFT JOIN turn_cost tc ON tc.task_id = fr.task_id
    LEFT JOIN merged m ON m.task_id = fr.task_id
    LEFT JOIN reverted rv ON rv.task_id = fr.task_id
    LEFT JOIN observability.merged_prs p ON p.number = m.pr_number
    WHERE fr.task_id IS NOT NULL AND impl.model IS NOT NULL
      AND fr.created_at >= :'window_start' AND fr.created_at < :'window_end'
)
SELECT model, size_band, COUNT(*) AS tasks,
       SUM((merged_at IS NOT NULL)::int) AS merged,
       -- Positive outcome: merged and not reverted. CI conclusion is reported
       -- alongside once landing records it; until then it is unknown, not red.
       SUM((merged_at IS NOT NULL AND NOT reverted)::int) AS positive,
       SUM((ci_conclusion = 'success')::int) AS ci_green,
       SUM(reverted::int) AS reverted,
       ROUND((SUM(cost) / NULLIF(SUM((merged_at IS NOT NULL AND NOT reverted)::int), 0))::numeric, 2)
           AS usd_per_positive,
       ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (
           ORDER BY EXTRACT(EPOCH FROM merged_at - created_at) / 3600))::numeric, 1)
           AS p50_hours_to_merge
FROM tasks
GROUP BY model, size_band
ORDER BY model, size_band;
