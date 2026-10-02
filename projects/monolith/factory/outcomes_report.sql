-- Factory outcomes report for #6716. PostgreSQL 16+, manually invoked, read-only.
-- psql "$DATABASE_URL" -X -v cohort_start='2026-09-07T00:00:00Z' \
--   -v cohort_end='2026-10-02T00:00:00Z' -v as_of='2026-10-02T00:00:00Z' \
--   -f projects/monolith/factory/outcomes_report.sql
-- Exactly three variables, each an explicit UTC timestamptz literal.
-- See outcomes.md for predicates, attribution, denominators and snapshot limits.
-- CTEs repeat deliberately: each echo section is one independently bindable SELECT.

SET default_transaction_read_only = on;
\echo 'cohort_start (UTC):' :'cohort_start'
\echo 'cohort_end (UTC, exclusive):' :'cohort_end'
\echo 'as_of (UTC, evidence exclusive):' :'as_of'

\echo '== Task outcomes by task class and initiating model'
WITH
params AS (
    SELECT :'cohort_start'::timestamptz AS cohort_start,
           :'cohort_end'::timestamptz AS cohort_end,
           :'as_of'::timestamptz AS as_of
),
cohort AS (
    SELECT fr.*, st.session_id AS task_session_id,
           CASE WHEN st.settled_at < p.as_of THEN st.settled_at END AS settled_at,
           CASE WHEN w.updated_at < p.as_of AND w.created_at < p.as_of
                     AND jsonb_typeof(w.labels) = 'array'
                     AND NOT EXISTS (
                         SELECT 1 FROM jsonb_array_elements(
                             CASE WHEN jsonb_typeof(w.labels) = 'array'
                                  THEN w.labels ELSE '[]'::jsonb END
                         ) label WHERE jsonb_typeof(label) <> 'string'
                     ) THEN w.labels END AS labels
    FROM swarm.factory_receipt fr
    CROSS JOIN params p
    LEFT JOIN swarm.swarm_task st ON st.id = fr.task_id AND st.created_at < p.as_of
    LEFT JOIN swarm.work_item w ON w.id = fr.work_item_id
    WHERE fr.task_id IS NOT NULL
      AND fr.created_at >= p.cohort_start AND fr.created_at < p.cohort_end
      AND fr.created_at < p.as_of
),
runs AS (
    SELECT r.id, r.task_id, r.node_key, r.attempt, r.dispatch_key, r.session_id,
           r.created_at,
           CASE WHEN r.finished_at < p.as_of THEN r.finished_at END AS finished_at,
           CASE WHEN r.finished_at >= p.as_of THEN 'pending' ELSE r.status END AS status,
           CASE WHEN starts_with(r.node_key, 'conductor_funding') THEN 'funding'
                ELSE split_part(r.node_key, '_', 1) END AS role,
           COALESCE(NULLIF(r.model, ''), NULLIF(j.pin ->> 'model', ''), 'unknown') AS model,
           (NULLIF(j.pin ->> 'escalated_from', '') IS NOT NULL) AS pool_escalated,
           j.outcome
    FROM swarm.swarm_node_run r
    JOIN cohort c ON c.task_id = r.task_id
    CROSS JOIN params p
    CROSS JOIN LATERAL (
        SELECT CASE WHEN pg_input_is_valid(r.pin_json, 'jsonb')
                    THEN r.pin_json::jsonb ELSE '{}'::jsonb END AS pin,
               CASE WHEN pg_input_is_valid(r.outcome_json, 'jsonb')
                    THEN r.outcome_json::jsonb ELSE '{}'::jsonb END AS outcome
    ) j
    WHERE r.created_at < p.as_of
),
starts AS (
    SELECT s.*, COALESCE(r.role, 'unassigned') AS role,
           CASE WHEN s.updated_at < p.as_of
                     AND s.status IN ('succeeded', 'failed', 'cancelled')
                THEN COALESCE(s.cost_usd, 0) ELSE 0 END AS settled_usd,
           CASE WHEN s.status IN ('reserved', 'uncertain')
                THEN GREATEST(s.max_cost_usd, COALESCE(s.cost_usd, 0))
                WHEN s.updated_at >= p.as_of
                THEN s.max_cost_usd
                WHEN s.updated_at < p.as_of
                     AND s.status IN ('succeeded', 'failed', 'cancelled')
                     AND s.cost_usd IS NULL
                     AND COALESCE(s.accounting_basis, '') NOT IN (
                         'no_model_post', 'capacity_denied', 'no_session_created')
                THEN s.max_cost_usd ELSE 0 END AS exposure_usd,
           (s.updated_at >= p.as_of OR s.status IN ('reserved', 'uncertain')) AS unsettled,
           (s.updated_at < p.as_of AND s.status IN ('succeeded', 'failed', 'cancelled')
                AND s.cost_usd IS NULL
                AND COALESCE(s.accounting_basis, '') NOT IN (
                    'no_model_post', 'capacity_denied', 'no_session_created')
                ) AS missing_settled_cost
    FROM swarm.factory_start s
    JOIN cohort c ON c.task_id = s.task_id
    CROSS JOIN params p
    LEFT JOIN runs r ON r.task_id = s.task_id AND r.dispatch_key = s.start_key
    WHERE s.created_at < p.as_of
),
session_links AS (
    -- UNION deduplicates a session reached through several ownership paths.
    SELECT task_id, session_id FROM runs WHERE session_id IS NOT NULL
    UNION
    SELECT task_id, session_id FROM starts WHERE session_id IS NOT NULL
    UNION
    SELECT task_id, task_session_id FROM cohort WHERE task_session_id IS NOT NULL
    UNION
    SELECT c.task_id, s.id
    FROM cohort c
    JOIN agent_sessions.agent_sessions s
      ON split_part(s.local_session_id, ':', 1) = 'factory'
     AND split_part(s.local_session_id, ':', 2) = c.task_id
    CROSS JOIN params p
    WHERE s.created_at < p.as_of
),
session_owners AS (
    -- One contribution owner per task/session, earliest run wins reused sessions.
    SELECT l.task_id, l.session_id,
           COALESCE(r.role, s.role,
                    CASE WHEN c.task_session_id = l.session_id THEN 'conductor' END,
                    'unassigned') AS role,
           COALESCE(r.model, s.model, 'unknown') AS model
    FROM session_links l
    JOIN cohort c ON c.task_id = l.task_id
    JOIN agent_sessions.agent_sessions a ON a.id = l.session_id
    CROSS JOIN params p
    LEFT JOIN LATERAL (
        SELECT role, model FROM runs r
        WHERE r.task_id = l.task_id AND r.session_id = l.session_id
        ORDER BY r.created_at, r.id LIMIT 1
    ) r ON true
    LEFT JOIN LATERAL (
        SELECT role, model FROM starts s
        WHERE s.task_id = l.task_id AND s.session_id = l.session_id
        ORDER BY s.created_at, s.id LIMIT 1
    ) s ON true
    WHERE a.created_at < p.as_of
),
turns AS (
    -- Unique turn IDs and one owner per task/session prevent duplicate paths.
    SELECT t.id, o.task_id, o.role,
           COALESCE(NULLIF(t.model, ''), o.model) AS model, t.list_cost_usd
    FROM session_owners o
    JOIN agent_sessions.agent_turns t ON t.session_id = o.session_id
    CROSS JOIN params p
    WHERE t.created_at < p.as_of
),
task_cost AS (
    SELECT task_id, COALESCE(SUM(list_cost_usd), 0) AS list_usd,
           COUNT(*) FILTER (WHERE list_cost_usd IS NULL) AS unpriced_turns,
           COUNT(*) AS turn_count
    FROM turns GROUP BY task_id
),
task_ledger AS (
    SELECT task_id, SUM(settled_usd) AS settled_usd, SUM(exposure_usd) AS exposure_usd,
           COUNT(*) FILTER (WHERE unsettled) AS unsettled_reservations,
           COUNT(*) FILTER (WHERE missing_settled_cost) AS missing_settled_costs
    FROM starts GROUP BY task_id
),
audits AS (
    SELECT a.id, a.task_id, a.action, a.created_at,
           CASE WHEN pg_input_is_valid(a.detail_json, 'jsonb')
                THEN a.detail_json::jsonb ELSE '{}'::jsonb END AS detail
    FROM swarm.factory_audit a
    JOIN cohort c ON c.task_id = a.task_id
    CROSS JOIN params p
    WHERE a.created_at < p.as_of
),
merge_audits AS (
    -- Collapse every audit kind before task joins. Earliest merge defines delivery.
    SELECT task_id, MIN(created_at) AS merged_at,
           (array_agg(detail ORDER BY created_at, id))[1] AS detail
    FROM audits WHERE action = 'merged' GROUP BY task_id
),
delivery_refs AS (
    SELECT task_id,
           substring(detail #>> '{evidence,pr_url}'
                     FROM '^https://github[.]com/jomcgi-org/homelab/pull/([0-9]+)/?$') AS pr
    FROM audits WHERE action IN ('finish_task', 'delivery_ready')
    UNION
    SELECT task_id, COALESCE(outcome #>> '{value,pr_number}', outcome ->> 'pr_number')
    FROM runs
    CROSS JOIN params p
    WHERE finished_at < p.as_of
),
historical_merges AS (
    SELECT d.task_id, MIN(m.merged_at) AS merged_at,
           (array_agg(m.number ORDER BY m.merged_at, m.number))[1]::text AS pr
    FROM delivery_refs d
    JOIN cohort c ON c.task_id = d.task_id AND c.repo = 'jomcgi-org/homelab'
    JOIN observability.merged_prs m ON m.number::text = d.pr
    CROSS JOIN params p
    WHERE m.merged_at < p.as_of
    GROUP BY d.task_id
),
merges AS (
    SELECT c.task_id, COALESCE(a.merged_at, h.merged_at) AS merged_at,
           CASE WHEN a.task_id IS NOT NULL THEN a.detail ->> 'pr_number' ELSE h.pr END AS pr,
           a.detail ->> 'merge_commit_sha' AS merge_sha,
           (a.task_id IS NULL AND h.task_id IS NOT NULL) AS historical_unjudged
    FROM cohort c
    LEFT JOIN merge_audits a ON a.task_id = c.task_id
    LEFT JOIN historical_merges h ON h.task_id = c.task_id
),
ci_audits AS (
    SELECT a.task_id,
           (array_agg(a.detail ->> 'conclusion' ORDER BY a.created_at, a.id))[1] AS conclusion
    FROM audits a
    JOIN merges m ON m.task_id = a.task_id
                 AND (m.merged_at IS NULL OR a.detail ->> 'pr_number' = m.pr)
    WHERE a.action = 'merge_ci'
      AND (m.merged_at IS NULL OR a.created_at >= m.merged_at)
      AND NULLIF(a.detail ->> 'pr_number', '') IS NOT NULL
      AND a.detail ->> 'head_sha' ~ '^[0-9a-f]{40}$'
    GROUP BY a.task_id
),
revert_audits AS (
    SELECT task_id, true AS reverted FROM audits
    WHERE action = 'reverted' GROUP BY task_id
),
window_audits AS (
    SELECT a.task_id, true AS window_closed
    FROM audits a
    JOIN merges m ON m.task_id = a.task_id
                 AND a.detail ->> 'pr_number' = m.pr
                 AND a.detail ->> 'merge_commit_sha' = m.merge_sha
    CROSS JOIN params p
    WHERE a.action = 'revert_window_closed'
      AND NULLIF(m.merge_sha, '') IS NOT NULL
      AND a.created_at >= m.merged_at + interval '7 days'
      AND m.merged_at + interval '7 days' <= p.as_of
    GROUP BY a.task_id
),
initiators AS (
    SELECT DISTINCT ON (task_id) task_id, model
    FROM runs WHERE role = 'implement'
    ORDER BY task_id, created_at, id
),
run_evidence AS (
    SELECT task_id, COUNT(DISTINCT node_key) FILTER (WHERE starts_with(node_key, 'correct_'))
               AS fixup_rounds,
           BOOL_OR(status = 'escalated') AS escalated,
           BOOL_OR(pool_escalated) AS pool_escalated,
           BOOL_OR(model = 'unknown') AS unknown_model
    FROM runs GROUP BY task_id
),
task_evidence AS (
    SELECT c.*, m.merged_at, m.pr, m.historical_unjudged,
           COALESCE(i.model, 'none') AS initiating_model,
           ci.conclusion AS ci_conclusion, COALESCE(rv.reverted, false) AS reverted,
           COALESCE(w.window_closed, false) AS window_closed,
           COALESCE(tc.list_usd, 0) AS list_usd,
           COALESCE(tc.unpriced_turns, 0) AS unpriced_turns,
           COALESCE(tc.turn_count, 0) AS turn_count,
           COALESCE(tl.settled_usd, 0) AS settled_usd,
           COALESCE(tl.exposure_usd, 0) AS exposure_usd,
           COALESCE(tl.unsettled_reservations, 0) AS unsettled_reservations,
           COALESCE(tl.missing_settled_costs, 0) AS missing_settled_costs,
           COALESCE(re.fixup_rounds, 0) AS fixup_rounds,
           (COALESCE(re.escalated, false)
            OR COALESCE(re.pool_escalated, false)
            OR (c.updated_at < p.as_of AND pg_input_is_valid(c.escalation_json, 'jsonb')
                AND CASE WHEN pg_input_is_valid(c.escalation_json, 'jsonb')
                         THEN c.escalation_json::jsonb ELSE '{}'::jsonb END
                    NOT IN ('{}'::jsonb, 'null'::jsonb, '[]'::jsonb, '""'::jsonb))) AS escalated,
           (c.updated_at >= p.as_of OR COALESCE(re.unknown_model, false)) AS escalation_unknown,
           mp.additions + mp.deletions AS changed_lines, mp.changed_files,
           EXTRACT(EPOCH FROM COALESCE(m.merged_at, c.settled_at) - c.created_at) / 3600
               AS elapsed_hours
    FROM cohort c CROSS JOIN params p
    LEFT JOIN merges m ON m.task_id = c.task_id
    LEFT JOIN initiators i ON i.task_id = c.task_id
    LEFT JOIN ci_audits ci ON ci.task_id = c.task_id
    LEFT JOIN revert_audits rv ON rv.task_id = c.task_id
    LEFT JOIN window_audits w ON w.task_id = c.task_id
    LEFT JOIN task_cost tc ON tc.task_id = c.task_id
    LEFT JOIN task_ledger tl ON tl.task_id = c.task_id
    LEFT JOIN run_evidence re ON re.task_id = c.task_id
    LEFT JOIN observability.merged_prs mp ON mp.number::text = m.pr AND mp.merged_at < p.as_of
),
tasks AS (
    SELECT e.*,
           CASE WHEN reverted THEN 'reverted'
                WHEN ci_conclusion = 'failure' THEN 'ci_failed'
                WHEN historical_unjudged THEN 'unknown'
                WHEN merged_at IS NOT NULL AND ci_conclusion = 'success' AND window_closed
                     AND merged_at + interval '7 days' <= p.as_of THEN 'positive'
                WHEN merged_at IS NOT NULL AND ci_conclusion = 'success' THEN 'pending_maturity'
                WHEN merged_at IS NOT NULL THEN 'unknown'
                WHEN settled_at IS NOT NULL THEN 'failed'
                ELSE 'censored' END AS outcome_state
    FROM task_evidence e CROSS JOIN params p
)
SELECT task_class, initiating_model, COUNT(*) AS tasks,
       COUNT(*) FILTER (WHERE outcome_state = 'positive') AS positives,
       COUNT(*) FILTER (WHERE outcome_state = 'reverted') AS reverted,
       COUNT(*) FILTER (WHERE outcome_state = 'ci_failed') AS ci_failed,
       COUNT(*) FILTER (WHERE outcome_state = 'pending_maturity') AS pending_maturity,
       COUNT(*) FILTER (WHERE outcome_state = 'unknown') AS unknown,
       COUNT(*) FILTER (WHERE outcome_state = 'failed') AS failed,
       COUNT(*) FILTER (WHERE outcome_state = 'censored') AS censored,
       SUM(list_usd) AS list_usd, SUM(unpriced_turns) AS unpriced_turns,
       SUM(settled_usd) AS settled_usd, SUM(exposure_usd) AS exposure_usd,
       SUM(settled_usd + exposure_usd) AS ledger_upper_usd,
       SUM(list_usd) / NULLIF(COUNT(*) FILTER (WHERE outcome_state = 'positive'), 0)
           AS usd_per_positive_lower,
       SUM(settled_usd + exposure_usd) / NULLIF(COUNT(*) FILTER (WHERE outcome_state = 'positive'), 0)
           AS usd_per_positive_upper,
       SUM(elapsed_hours) / NULLIF(COUNT(*) FILTER (WHERE outcome_state = 'positive'), 0)
           AS hours_per_positive,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_hours)
           FILTER (WHERE merged_at IS NOT NULL) AS p50_hours_to_merge,
       COUNT(*) FILTER (WHERE outcome_state = 'positive') AS success_numerator,
       COUNT(*) FILTER (WHERE outcome_state IN ('positive', 'reverted', 'ci_failed', 'failed'))
           AS success_denominator,
       COUNT(*) FILTER (WHERE outcome_state IN ('pending_maturity', 'unknown', 'censored'))
           AS success_unknown_count,
       1.0 * COUNT(*) FILTER (WHERE outcome_state = 'positive')
           / NULLIF(COUNT(*) FILTER (WHERE outcome_state IN ('positive', 'reverted', 'ci_failed', 'failed')), 0)
           AS success_rate,
       0 AS first_pass_ci_numerator, 0 AS first_pass_ci_denominator,
       COUNT(*) AS first_pass_ci_unknown_count, NULL::numeric AS first_pass_ci_rate,
       SUM(fixup_rounds) AS fixup_rounds_total, COUNT(*) AS fixup_rounds_denominator,
       0 AS fixup_rounds_unknown_count, AVG(fixup_rounds) AS fixup_rounds_mean,
       COUNT(*) FILTER (WHERE escalated) AS escalation_numerator,
       COUNT(*) FILTER (WHERE escalated OR NOT escalation_unknown) AS escalation_denominator,
       COUNT(*) FILTER (WHERE NOT escalated AND escalation_unknown) AS escalation_unknown_count,
       1.0 * COUNT(*) FILTER (WHERE escalated)
           / NULLIF(COUNT(*) FILTER (WHERE escalated OR NOT escalation_unknown), 0) AS escalation_rate,
       COUNT(*) FILTER (WHERE merged_at IS NOT NULL AND reverted) AS revert_numerator,
       COUNT(*) FILTER (WHERE merged_at IS NOT NULL AND (reverted OR window_closed)) AS revert_denominator,
       COUNT(*) FILTER (WHERE merged_at IS NOT NULL AND NOT (reverted OR window_closed)) AS revert_unknown_count,
       1.0 * COUNT(*) FILTER (WHERE merged_at IS NOT NULL AND reverted)
           / NULLIF(COUNT(*) FILTER (WHERE merged_at IS NOT NULL AND (reverted OR window_closed)), 0)
           AS revert_rate
FROM tasks GROUP BY task_class, initiating_model ORDER BY task_class, initiating_model;

\echo '== Contributions by task class role and actual model'
WITH
params AS (
    SELECT :'cohort_start'::timestamptz AS cohort_start,
           :'cohort_end'::timestamptz AS cohort_end,
           :'as_of'::timestamptz AS as_of
),
cohort AS (
    SELECT fr.*, st.session_id AS task_session_id,
           CASE WHEN st.settled_at < p.as_of THEN st.settled_at END AS settled_at,
           CASE WHEN w.updated_at < p.as_of AND w.created_at < p.as_of
                     AND jsonb_typeof(w.labels) = 'array'
                     AND NOT EXISTS (
                         SELECT 1 FROM jsonb_array_elements(
                             CASE WHEN jsonb_typeof(w.labels) = 'array'
                                  THEN w.labels ELSE '[]'::jsonb END
                         ) label WHERE jsonb_typeof(label) <> 'string'
                     ) THEN w.labels END AS labels
    FROM swarm.factory_receipt fr
    CROSS JOIN params p
    LEFT JOIN swarm.swarm_task st ON st.id = fr.task_id AND st.created_at < p.as_of
    LEFT JOIN swarm.work_item w ON w.id = fr.work_item_id
    WHERE fr.task_id IS NOT NULL
      AND fr.created_at >= p.cohort_start AND fr.created_at < p.cohort_end
      AND fr.created_at < p.as_of
),
runs AS (
    SELECT r.id, r.task_id, r.node_key, r.attempt, r.dispatch_key, r.session_id,
           r.created_at,
           CASE WHEN r.finished_at < p.as_of THEN r.finished_at END AS finished_at,
           CASE WHEN r.finished_at >= p.as_of THEN 'pending' ELSE r.status END AS status,
           CASE WHEN starts_with(r.node_key, 'conductor_funding') THEN 'funding'
                ELSE split_part(r.node_key, '_', 1) END AS role,
           COALESCE(NULLIF(r.model, ''), NULLIF(j.pin ->> 'model', ''), 'unknown') AS model,
           (NULLIF(j.pin ->> 'escalated_from', '') IS NOT NULL) AS pool_escalated,
           j.outcome
    FROM swarm.swarm_node_run r
    JOIN cohort c ON c.task_id = r.task_id
    CROSS JOIN params p
    CROSS JOIN LATERAL (
        SELECT CASE WHEN pg_input_is_valid(r.pin_json, 'jsonb')
                    THEN r.pin_json::jsonb ELSE '{}'::jsonb END AS pin,
               CASE WHEN pg_input_is_valid(r.outcome_json, 'jsonb')
                    THEN r.outcome_json::jsonb ELSE '{}'::jsonb END AS outcome
    ) j
    WHERE r.created_at < p.as_of
),
starts AS (
    SELECT s.*, COALESCE(r.role, 'unassigned') AS role,
           CASE WHEN s.updated_at < p.as_of
                     AND s.status IN ('succeeded', 'failed', 'cancelled')
                THEN COALESCE(s.cost_usd, 0) ELSE 0 END AS settled_usd,
           CASE WHEN s.status IN ('reserved', 'uncertain')
                THEN GREATEST(s.max_cost_usd, COALESCE(s.cost_usd, 0))
                WHEN s.updated_at >= p.as_of
                THEN s.max_cost_usd
                WHEN s.updated_at < p.as_of
                     AND s.status IN ('succeeded', 'failed', 'cancelled')
                     AND s.cost_usd IS NULL
                     AND COALESCE(s.accounting_basis, '') NOT IN (
                         'no_model_post', 'capacity_denied', 'no_session_created')
                THEN s.max_cost_usd ELSE 0 END AS exposure_usd,
           (s.updated_at >= p.as_of OR s.status IN ('reserved', 'uncertain')) AS unsettled,
           (s.updated_at < p.as_of AND s.status IN ('succeeded', 'failed', 'cancelled')
                AND s.cost_usd IS NULL
                AND COALESCE(s.accounting_basis, '') NOT IN (
                    'no_model_post', 'capacity_denied', 'no_session_created')
                ) AS missing_settled_cost
    FROM swarm.factory_start s
    JOIN cohort c ON c.task_id = s.task_id
    CROSS JOIN params p
    LEFT JOIN runs r ON r.task_id = s.task_id AND r.dispatch_key = s.start_key
    WHERE s.created_at < p.as_of
),
session_links AS (
    -- UNION deduplicates a session reached through several ownership paths.
    SELECT task_id, session_id FROM runs WHERE session_id IS NOT NULL
    UNION
    SELECT task_id, session_id FROM starts WHERE session_id IS NOT NULL
    UNION
    SELECT task_id, task_session_id FROM cohort WHERE task_session_id IS NOT NULL
    UNION
    SELECT c.task_id, s.id
    FROM cohort c
    JOIN agent_sessions.agent_sessions s
      ON split_part(s.local_session_id, ':', 1) = 'factory'
     AND split_part(s.local_session_id, ':', 2) = c.task_id
    CROSS JOIN params p
    WHERE s.created_at < p.as_of
),
session_owners AS (
    -- One contribution owner per task/session, earliest run wins reused sessions.
    SELECT l.task_id, l.session_id,
           COALESCE(r.role, s.role,
                    CASE WHEN c.task_session_id = l.session_id THEN 'conductor' END,
                    'unassigned') AS role,
           COALESCE(r.model, s.model, 'unknown') AS model
    FROM session_links l
    JOIN cohort c ON c.task_id = l.task_id
    JOIN agent_sessions.agent_sessions a ON a.id = l.session_id
    CROSS JOIN params p
    LEFT JOIN LATERAL (
        SELECT role, model FROM runs r
        WHERE r.task_id = l.task_id AND r.session_id = l.session_id
        ORDER BY r.created_at, r.id LIMIT 1
    ) r ON true
    LEFT JOIN LATERAL (
        SELECT role, model FROM starts s
        WHERE s.task_id = l.task_id AND s.session_id = l.session_id
        ORDER BY s.created_at, s.id LIMIT 1
    ) s ON true
    WHERE a.created_at < p.as_of
),
turns AS (
    -- Unique turn IDs and one owner per task/session prevent duplicate paths.
    SELECT t.id, o.task_id, o.role,
           COALESCE(NULLIF(t.model, ''), o.model) AS model, t.list_cost_usd
    FROM session_owners o
    JOIN agent_sessions.agent_turns t ON t.session_id = o.session_id
    CROSS JOIN params p
    WHERE t.created_at < p.as_of
),
task_cost AS (
    SELECT task_id, COALESCE(SUM(list_cost_usd), 0) AS list_usd,
           COUNT(*) FILTER (WHERE list_cost_usd IS NULL) AS unpriced_turns,
           COUNT(*) AS turn_count
    FROM turns GROUP BY task_id
),
task_ledger AS (
    SELECT task_id, SUM(settled_usd) AS settled_usd, SUM(exposure_usd) AS exposure_usd,
           COUNT(*) FILTER (WHERE unsettled) AS unsettled_reservations,
           COUNT(*) FILTER (WHERE missing_settled_cost) AS missing_settled_costs
    FROM starts GROUP BY task_id
),
audits AS (
    SELECT a.id, a.task_id, a.action, a.created_at,
           CASE WHEN pg_input_is_valid(a.detail_json, 'jsonb')
                THEN a.detail_json::jsonb ELSE '{}'::jsonb END AS detail
    FROM swarm.factory_audit a
    JOIN cohort c ON c.task_id = a.task_id
    CROSS JOIN params p
    WHERE a.created_at < p.as_of
),
merge_audits AS (
    -- Collapse every audit kind before task joins. Earliest merge defines delivery.
    SELECT task_id, MIN(created_at) AS merged_at,
           (array_agg(detail ORDER BY created_at, id))[1] AS detail
    FROM audits WHERE action = 'merged' GROUP BY task_id
),
delivery_refs AS (
    SELECT task_id,
           substring(detail #>> '{evidence,pr_url}'
                     FROM '^https://github[.]com/jomcgi-org/homelab/pull/([0-9]+)/?$') AS pr
    FROM audits WHERE action IN ('finish_task', 'delivery_ready')
    UNION
    SELECT task_id, COALESCE(outcome #>> '{value,pr_number}', outcome ->> 'pr_number')
    FROM runs
    CROSS JOIN params p
    WHERE finished_at < p.as_of
),
historical_merges AS (
    SELECT d.task_id, MIN(m.merged_at) AS merged_at,
           (array_agg(m.number ORDER BY m.merged_at, m.number))[1]::text AS pr
    FROM delivery_refs d
    JOIN cohort c ON c.task_id = d.task_id AND c.repo = 'jomcgi-org/homelab'
    JOIN observability.merged_prs m ON m.number::text = d.pr
    CROSS JOIN params p
    WHERE m.merged_at < p.as_of
    GROUP BY d.task_id
),
merges AS (
    SELECT c.task_id, COALESCE(a.merged_at, h.merged_at) AS merged_at,
           CASE WHEN a.task_id IS NOT NULL THEN a.detail ->> 'pr_number' ELSE h.pr END AS pr,
           a.detail ->> 'merge_commit_sha' AS merge_sha,
           (a.task_id IS NULL AND h.task_id IS NOT NULL) AS historical_unjudged
    FROM cohort c
    LEFT JOIN merge_audits a ON a.task_id = c.task_id
    LEFT JOIN historical_merges h ON h.task_id = c.task_id
),
ci_audits AS (
    SELECT a.task_id,
           (array_agg(a.detail ->> 'conclusion' ORDER BY a.created_at, a.id))[1] AS conclusion
    FROM audits a
    JOIN merges m ON m.task_id = a.task_id
                 AND (m.merged_at IS NULL OR a.detail ->> 'pr_number' = m.pr)
    WHERE a.action = 'merge_ci'
      AND (m.merged_at IS NULL OR a.created_at >= m.merged_at)
      AND NULLIF(a.detail ->> 'pr_number', '') IS NOT NULL
      AND a.detail ->> 'head_sha' ~ '^[0-9a-f]{40}$'
    GROUP BY a.task_id
),
revert_audits AS (
    SELECT task_id, true AS reverted FROM audits
    WHERE action = 'reverted' GROUP BY task_id
),
window_audits AS (
    SELECT a.task_id, true AS window_closed
    FROM audits a
    JOIN merges m ON m.task_id = a.task_id
                 AND a.detail ->> 'pr_number' = m.pr
                 AND a.detail ->> 'merge_commit_sha' = m.merge_sha
    CROSS JOIN params p
    WHERE a.action = 'revert_window_closed'
      AND NULLIF(m.merge_sha, '') IS NOT NULL
      AND a.created_at >= m.merged_at + interval '7 days'
      AND m.merged_at + interval '7 days' <= p.as_of
    GROUP BY a.task_id
),
initiators AS (
    SELECT DISTINCT ON (task_id) task_id, model
    FROM runs WHERE role = 'implement'
    ORDER BY task_id, created_at, id
),
run_evidence AS (
    SELECT task_id, COUNT(DISTINCT node_key) FILTER (WHERE starts_with(node_key, 'correct_'))
               AS fixup_rounds,
           BOOL_OR(status = 'escalated') AS escalated,
           BOOL_OR(pool_escalated) AS pool_escalated,
           BOOL_OR(model = 'unknown') AS unknown_model
    FROM runs GROUP BY task_id
),
task_evidence AS (
    SELECT c.*, m.merged_at, m.pr, m.historical_unjudged,
           COALESCE(i.model, 'none') AS initiating_model,
           ci.conclusion AS ci_conclusion, COALESCE(rv.reverted, false) AS reverted,
           COALESCE(w.window_closed, false) AS window_closed,
           COALESCE(tc.list_usd, 0) AS list_usd,
           COALESCE(tc.unpriced_turns, 0) AS unpriced_turns,
           COALESCE(tc.turn_count, 0) AS turn_count,
           COALESCE(tl.settled_usd, 0) AS settled_usd,
           COALESCE(tl.exposure_usd, 0) AS exposure_usd,
           COALESCE(tl.unsettled_reservations, 0) AS unsettled_reservations,
           COALESCE(tl.missing_settled_costs, 0) AS missing_settled_costs,
           COALESCE(re.fixup_rounds, 0) AS fixup_rounds,
           (COALESCE(re.escalated, false)
            OR COALESCE(re.pool_escalated, false)
            OR (c.updated_at < p.as_of AND pg_input_is_valid(c.escalation_json, 'jsonb')
                AND CASE WHEN pg_input_is_valid(c.escalation_json, 'jsonb')
                         THEN c.escalation_json::jsonb ELSE '{}'::jsonb END
                    NOT IN ('{}'::jsonb, 'null'::jsonb, '[]'::jsonb, '""'::jsonb))) AS escalated,
           (c.updated_at >= p.as_of OR COALESCE(re.unknown_model, false)) AS escalation_unknown,
           mp.additions + mp.deletions AS changed_lines, mp.changed_files,
           EXTRACT(EPOCH FROM COALESCE(m.merged_at, c.settled_at) - c.created_at) / 3600
               AS elapsed_hours
    FROM cohort c CROSS JOIN params p
    LEFT JOIN merges m ON m.task_id = c.task_id
    LEFT JOIN initiators i ON i.task_id = c.task_id
    LEFT JOIN ci_audits ci ON ci.task_id = c.task_id
    LEFT JOIN revert_audits rv ON rv.task_id = c.task_id
    LEFT JOIN window_audits w ON w.task_id = c.task_id
    LEFT JOIN task_cost tc ON tc.task_id = c.task_id
    LEFT JOIN task_ledger tl ON tl.task_id = c.task_id
    LEFT JOIN run_evidence re ON re.task_id = c.task_id
    LEFT JOIN observability.merged_prs mp ON mp.number::text = m.pr AND mp.merged_at < p.as_of
),
tasks AS (
    SELECT e.*,
           CASE WHEN reverted THEN 'reverted'
                WHEN ci_conclusion = 'failure' THEN 'ci_failed'
                WHEN historical_unjudged THEN 'unknown'
                WHEN merged_at IS NOT NULL AND ci_conclusion = 'success' AND window_closed
                     AND merged_at + interval '7 days' <= p.as_of THEN 'positive'
                WHEN merged_at IS NOT NULL AND ci_conclusion = 'success' THEN 'pending_maturity'
                WHEN merged_at IS NOT NULL THEN 'unknown'
                WHEN settled_at IS NOT NULL THEN 'failed'
                ELSE 'censored' END AS outcome_state
    FROM task_evidence e CROSS JOIN params p
),
contributions AS (
    -- Each source aggregates independently, so ledger and turns never fan out.
    SELECT c.task_class, r.task_id, r.role, r.model, 1 AS attempts,
           (r.status = 'succeeded')::int AS succeeded,
           (r.status IN ('succeeded', 'failed', 'escalated', 'cancelled'))::int AS judged,
           0::double precision AS list_usd, 0 AS unpriced_turns,
           0::double precision AS settled_usd, 0::double precision AS exposure_usd,
           EXTRACT(EPOCH FROM r.finished_at - r.created_at) / 60 AS minutes
    FROM runs r JOIN cohort c ON c.task_id = r.task_id
    UNION ALL
    SELECT c.task_class, t.task_id, t.role, t.model, 0, 0, 0,
           COALESCE(t.list_cost_usd, 0), (t.list_cost_usd IS NULL)::int, 0, 0, NULL
    FROM turns t JOIN cohort c ON c.task_id = t.task_id
    UNION ALL
    SELECT c.task_class, s.task_id, s.role, s.model, 0, 0, 0,
           0, 0, s.settled_usd, s.exposure_usd, NULL
    FROM starts s JOIN cohort c ON c.task_id = s.task_id
)
SELECT task_class, role, model, SUM(attempts) AS attempts,
       SUM(succeeded) AS attempt_success_numerator, SUM(judged) AS attempt_success_denominator,
       SUM(attempts - judged) AS attempt_success_unknown_count,
       1.0 * SUM(succeeded) / NULLIF(SUM(judged), 0) AS attempt_success_rate,
       COUNT(DISTINCT task_id) AS distinct_tasks,
       SUM(list_usd) AS list_usd, SUM(unpriced_turns) AS unpriced_turns,
       SUM(settled_usd) AS settled_usd, SUM(exposure_usd) AS exposure_usd,
       SUM(settled_usd + exposure_usd) AS ledger_upper_usd, SUM(minutes) AS minutes
FROM contributions GROUP BY task_class, role, model ORDER BY task_class, role, model;

\echo '== Difficulty bands'
WITH
params AS (
    SELECT :'cohort_start'::timestamptz AS cohort_start,
           :'cohort_end'::timestamptz AS cohort_end,
           :'as_of'::timestamptz AS as_of
),
cohort AS (
    SELECT fr.*, st.session_id AS task_session_id,
           CASE WHEN st.settled_at < p.as_of THEN st.settled_at END AS settled_at,
           CASE WHEN w.updated_at < p.as_of AND w.created_at < p.as_of
                     AND jsonb_typeof(w.labels) = 'array'
                     AND NOT EXISTS (
                         SELECT 1 FROM jsonb_array_elements(
                             CASE WHEN jsonb_typeof(w.labels) = 'array'
                                  THEN w.labels ELSE '[]'::jsonb END
                         ) label WHERE jsonb_typeof(label) <> 'string'
                     ) THEN w.labels END AS labels
    FROM swarm.factory_receipt fr
    CROSS JOIN params p
    LEFT JOIN swarm.swarm_task st ON st.id = fr.task_id AND st.created_at < p.as_of
    LEFT JOIN swarm.work_item w ON w.id = fr.work_item_id
    WHERE fr.task_id IS NOT NULL
      AND fr.created_at >= p.cohort_start AND fr.created_at < p.cohort_end
      AND fr.created_at < p.as_of
),
runs AS (
    SELECT r.id, r.task_id, r.node_key, r.attempt, r.dispatch_key, r.session_id,
           r.created_at,
           CASE WHEN r.finished_at < p.as_of THEN r.finished_at END AS finished_at,
           CASE WHEN r.finished_at >= p.as_of THEN 'pending' ELSE r.status END AS status,
           CASE WHEN starts_with(r.node_key, 'conductor_funding') THEN 'funding'
                ELSE split_part(r.node_key, '_', 1) END AS role,
           COALESCE(NULLIF(r.model, ''), NULLIF(j.pin ->> 'model', ''), 'unknown') AS model,
           (NULLIF(j.pin ->> 'escalated_from', '') IS NOT NULL) AS pool_escalated,
           j.outcome
    FROM swarm.swarm_node_run r
    JOIN cohort c ON c.task_id = r.task_id
    CROSS JOIN params p
    CROSS JOIN LATERAL (
        SELECT CASE WHEN pg_input_is_valid(r.pin_json, 'jsonb')
                    THEN r.pin_json::jsonb ELSE '{}'::jsonb END AS pin,
               CASE WHEN pg_input_is_valid(r.outcome_json, 'jsonb')
                    THEN r.outcome_json::jsonb ELSE '{}'::jsonb END AS outcome
    ) j
    WHERE r.created_at < p.as_of
),
starts AS (
    SELECT s.*, COALESCE(r.role, 'unassigned') AS role,
           CASE WHEN s.updated_at < p.as_of
                     AND s.status IN ('succeeded', 'failed', 'cancelled')
                THEN COALESCE(s.cost_usd, 0) ELSE 0 END AS settled_usd,
           CASE WHEN s.status IN ('reserved', 'uncertain')
                THEN GREATEST(s.max_cost_usd, COALESCE(s.cost_usd, 0))
                WHEN s.updated_at >= p.as_of
                THEN s.max_cost_usd
                WHEN s.updated_at < p.as_of
                     AND s.status IN ('succeeded', 'failed', 'cancelled')
                     AND s.cost_usd IS NULL
                     AND COALESCE(s.accounting_basis, '') NOT IN (
                         'no_model_post', 'capacity_denied', 'no_session_created')
                THEN s.max_cost_usd ELSE 0 END AS exposure_usd,
           (s.updated_at >= p.as_of OR s.status IN ('reserved', 'uncertain')) AS unsettled,
           (s.updated_at < p.as_of AND s.status IN ('succeeded', 'failed', 'cancelled')
                AND s.cost_usd IS NULL
                AND COALESCE(s.accounting_basis, '') NOT IN (
                    'no_model_post', 'capacity_denied', 'no_session_created')
                ) AS missing_settled_cost
    FROM swarm.factory_start s
    JOIN cohort c ON c.task_id = s.task_id
    CROSS JOIN params p
    LEFT JOIN runs r ON r.task_id = s.task_id AND r.dispatch_key = s.start_key
    WHERE s.created_at < p.as_of
),
session_links AS (
    -- UNION deduplicates a session reached through several ownership paths.
    SELECT task_id, session_id FROM runs WHERE session_id IS NOT NULL
    UNION
    SELECT task_id, session_id FROM starts WHERE session_id IS NOT NULL
    UNION
    SELECT task_id, task_session_id FROM cohort WHERE task_session_id IS NOT NULL
    UNION
    SELECT c.task_id, s.id
    FROM cohort c
    JOIN agent_sessions.agent_sessions s
      ON split_part(s.local_session_id, ':', 1) = 'factory'
     AND split_part(s.local_session_id, ':', 2) = c.task_id
    CROSS JOIN params p
    WHERE s.created_at < p.as_of
),
session_owners AS (
    -- One contribution owner per task/session, earliest run wins reused sessions.
    SELECT l.task_id, l.session_id,
           COALESCE(r.role, s.role,
                    CASE WHEN c.task_session_id = l.session_id THEN 'conductor' END,
                    'unassigned') AS role,
           COALESCE(r.model, s.model, 'unknown') AS model
    FROM session_links l
    JOIN cohort c ON c.task_id = l.task_id
    JOIN agent_sessions.agent_sessions a ON a.id = l.session_id
    CROSS JOIN params p
    LEFT JOIN LATERAL (
        SELECT role, model FROM runs r
        WHERE r.task_id = l.task_id AND r.session_id = l.session_id
        ORDER BY r.created_at, r.id LIMIT 1
    ) r ON true
    LEFT JOIN LATERAL (
        SELECT role, model FROM starts s
        WHERE s.task_id = l.task_id AND s.session_id = l.session_id
        ORDER BY s.created_at, s.id LIMIT 1
    ) s ON true
    WHERE a.created_at < p.as_of
),
turns AS (
    -- Unique turn IDs and one owner per task/session prevent duplicate paths.
    SELECT t.id, o.task_id, o.role,
           COALESCE(NULLIF(t.model, ''), o.model) AS model, t.list_cost_usd
    FROM session_owners o
    JOIN agent_sessions.agent_turns t ON t.session_id = o.session_id
    CROSS JOIN params p
    WHERE t.created_at < p.as_of
),
task_cost AS (
    SELECT task_id, COALESCE(SUM(list_cost_usd), 0) AS list_usd,
           COUNT(*) FILTER (WHERE list_cost_usd IS NULL) AS unpriced_turns,
           COUNT(*) AS turn_count
    FROM turns GROUP BY task_id
),
task_ledger AS (
    SELECT task_id, SUM(settled_usd) AS settled_usd, SUM(exposure_usd) AS exposure_usd,
           COUNT(*) FILTER (WHERE unsettled) AS unsettled_reservations,
           COUNT(*) FILTER (WHERE missing_settled_cost) AS missing_settled_costs
    FROM starts GROUP BY task_id
),
audits AS (
    SELECT a.id, a.task_id, a.action, a.created_at,
           CASE WHEN pg_input_is_valid(a.detail_json, 'jsonb')
                THEN a.detail_json::jsonb ELSE '{}'::jsonb END AS detail
    FROM swarm.factory_audit a
    JOIN cohort c ON c.task_id = a.task_id
    CROSS JOIN params p
    WHERE a.created_at < p.as_of
),
merge_audits AS (
    -- Collapse every audit kind before task joins. Earliest merge defines delivery.
    SELECT task_id, MIN(created_at) AS merged_at,
           (array_agg(detail ORDER BY created_at, id))[1] AS detail
    FROM audits WHERE action = 'merged' GROUP BY task_id
),
delivery_refs AS (
    SELECT task_id,
           substring(detail #>> '{evidence,pr_url}'
                     FROM '^https://github[.]com/jomcgi-org/homelab/pull/([0-9]+)/?$') AS pr
    FROM audits WHERE action IN ('finish_task', 'delivery_ready')
    UNION
    SELECT task_id, COALESCE(outcome #>> '{value,pr_number}', outcome ->> 'pr_number')
    FROM runs
    CROSS JOIN params p
    WHERE finished_at < p.as_of
),
historical_merges AS (
    SELECT d.task_id, MIN(m.merged_at) AS merged_at,
           (array_agg(m.number ORDER BY m.merged_at, m.number))[1]::text AS pr
    FROM delivery_refs d
    JOIN cohort c ON c.task_id = d.task_id AND c.repo = 'jomcgi-org/homelab'
    JOIN observability.merged_prs m ON m.number::text = d.pr
    CROSS JOIN params p
    WHERE m.merged_at < p.as_of
    GROUP BY d.task_id
),
merges AS (
    SELECT c.task_id, COALESCE(a.merged_at, h.merged_at) AS merged_at,
           CASE WHEN a.task_id IS NOT NULL THEN a.detail ->> 'pr_number' ELSE h.pr END AS pr,
           a.detail ->> 'merge_commit_sha' AS merge_sha,
           (a.task_id IS NULL AND h.task_id IS NOT NULL) AS historical_unjudged
    FROM cohort c
    LEFT JOIN merge_audits a ON a.task_id = c.task_id
    LEFT JOIN historical_merges h ON h.task_id = c.task_id
),
ci_audits AS (
    SELECT a.task_id,
           (array_agg(a.detail ->> 'conclusion' ORDER BY a.created_at, a.id))[1] AS conclusion
    FROM audits a
    JOIN merges m ON m.task_id = a.task_id
                 AND (m.merged_at IS NULL OR a.detail ->> 'pr_number' = m.pr)
    WHERE a.action = 'merge_ci'
      AND (m.merged_at IS NULL OR a.created_at >= m.merged_at)
      AND NULLIF(a.detail ->> 'pr_number', '') IS NOT NULL
      AND a.detail ->> 'head_sha' ~ '^[0-9a-f]{40}$'
    GROUP BY a.task_id
),
revert_audits AS (
    SELECT task_id, true AS reverted FROM audits
    WHERE action = 'reverted' GROUP BY task_id
),
window_audits AS (
    SELECT a.task_id, true AS window_closed
    FROM audits a
    JOIN merges m ON m.task_id = a.task_id
                 AND a.detail ->> 'pr_number' = m.pr
                 AND a.detail ->> 'merge_commit_sha' = m.merge_sha
    CROSS JOIN params p
    WHERE a.action = 'revert_window_closed'
      AND NULLIF(m.merge_sha, '') IS NOT NULL
      AND a.created_at >= m.merged_at + interval '7 days'
      AND m.merged_at + interval '7 days' <= p.as_of
    GROUP BY a.task_id
),
initiators AS (
    SELECT DISTINCT ON (task_id) task_id, model
    FROM runs WHERE role = 'implement'
    ORDER BY task_id, created_at, id
),
run_evidence AS (
    SELECT task_id, COUNT(DISTINCT node_key) FILTER (WHERE starts_with(node_key, 'correct_'))
               AS fixup_rounds,
           BOOL_OR(status = 'escalated') AS escalated,
           BOOL_OR(pool_escalated) AS pool_escalated,
           BOOL_OR(model = 'unknown') AS unknown_model
    FROM runs GROUP BY task_id
),
task_evidence AS (
    SELECT c.*, m.merged_at, m.pr, m.historical_unjudged,
           COALESCE(i.model, 'none') AS initiating_model,
           ci.conclusion AS ci_conclusion, COALESCE(rv.reverted, false) AS reverted,
           COALESCE(w.window_closed, false) AS window_closed,
           COALESCE(tc.list_usd, 0) AS list_usd,
           COALESCE(tc.unpriced_turns, 0) AS unpriced_turns,
           COALESCE(tc.turn_count, 0) AS turn_count,
           COALESCE(tl.settled_usd, 0) AS settled_usd,
           COALESCE(tl.exposure_usd, 0) AS exposure_usd,
           COALESCE(tl.unsettled_reservations, 0) AS unsettled_reservations,
           COALESCE(tl.missing_settled_costs, 0) AS missing_settled_costs,
           COALESCE(re.fixup_rounds, 0) AS fixup_rounds,
           (COALESCE(re.escalated, false)
            OR COALESCE(re.pool_escalated, false)
            OR (c.updated_at < p.as_of AND pg_input_is_valid(c.escalation_json, 'jsonb')
                AND CASE WHEN pg_input_is_valid(c.escalation_json, 'jsonb')
                         THEN c.escalation_json::jsonb ELSE '{}'::jsonb END
                    NOT IN ('{}'::jsonb, 'null'::jsonb, '[]'::jsonb, '""'::jsonb))) AS escalated,
           (c.updated_at >= p.as_of OR COALESCE(re.unknown_model, false)) AS escalation_unknown,
           mp.additions + mp.deletions AS changed_lines, mp.changed_files,
           EXTRACT(EPOCH FROM COALESCE(m.merged_at, c.settled_at) - c.created_at) / 3600
               AS elapsed_hours
    FROM cohort c CROSS JOIN params p
    LEFT JOIN merges m ON m.task_id = c.task_id
    LEFT JOIN initiators i ON i.task_id = c.task_id
    LEFT JOIN ci_audits ci ON ci.task_id = c.task_id
    LEFT JOIN revert_audits rv ON rv.task_id = c.task_id
    LEFT JOIN window_audits w ON w.task_id = c.task_id
    LEFT JOIN task_cost tc ON tc.task_id = c.task_id
    LEFT JOIN task_ledger tl ON tl.task_id = c.task_id
    LEFT JOIN run_evidence re ON re.task_id = c.task_id
    LEFT JOIN observability.merged_prs mp ON mp.number::text = m.pr AND mp.merged_at < p.as_of
),
tasks AS (
    SELECT e.*,
           CASE WHEN reverted THEN 'reverted'
                WHEN ci_conclusion = 'failure' THEN 'ci_failed'
                WHEN historical_unjudged THEN 'unknown'
                WHEN merged_at IS NOT NULL AND ci_conclusion = 'success' AND window_closed
                     AND merged_at + interval '7 days' <= p.as_of THEN 'positive'
                WHEN merged_at IS NOT NULL AND ci_conclusion = 'success' THEN 'pending_maturity'
                WHEN merged_at IS NOT NULL THEN 'unknown'
                WHEN settled_at IS NOT NULL THEN 'failed'
                ELSE 'censored' END AS outcome_state
    FROM task_evidence e CROSS JOIN params p
),
bands AS (
    SELECT t.*, b.dimension, b.band
    FROM tasks t
    CROSS JOIN LATERAL (VALUES
        ('diff_size', CASE WHEN merged_at IS NULL OR changed_lines IS NULL THEN 'unknown'
                          WHEN changed_lines < 50 THEN 'S' WHEN changed_lines < 300 THEN 'M' ELSE 'L' END),
        ('changed_files', CASE WHEN merged_at IS NULL OR changed_files IS NULL THEN 'unknown'
                              WHEN changed_files < 2 THEN 'S' WHEN changed_files < 6 THEN 'M' ELSE 'L' END),
        ('labels', CASE WHEN merged_at IS NULL OR labels IS NULL THEN 'unknown'
                       ELSE COALESCE((SELECT jsonb_agg(label ORDER BY label)::text
                                      FROM (SELECT DISTINCT jsonb_array_elements_text(labels) AS label) l), '[]') END),
        ('turn_count', CASE WHEN merged_at IS NULL THEN 'unknown'
                           WHEN turn_count < 10 THEN 'S' WHEN turn_count < 50 THEN 'M' ELSE 'L' END)
    ) b(dimension, band)
)
SELECT task_class, initiating_model, dimension, band, COUNT(*) AS tasks,
       COUNT(*) FILTER (WHERE outcome_state = 'positive') AS positives,
       COUNT(*) FILTER (WHERE outcome_state = 'censored') AS censored,
       SUM(list_usd) AS list_usd, SUM(unpriced_turns) AS unpriced_turns,
       SUM(exposure_usd) AS exposure_usd, SUM(settled_usd + exposure_usd) AS ledger_upper_usd,
       SUM(list_usd) / NULLIF(COUNT(*) FILTER (WHERE outcome_state = 'positive'), 0)
           AS usd_per_positive_lower,
       SUM(settled_usd + exposure_usd) / NULLIF(COUNT(*) FILTER (WHERE outcome_state = 'positive'), 0)
           AS usd_per_positive_upper,
       SUM(elapsed_hours) / NULLIF(COUNT(*) FILTER (WHERE outcome_state = 'positive'), 0)
           AS hours_per_positive
FROM bands GROUP BY task_class, initiating_model, dimension, band
ORDER BY task_class, initiating_model, dimension, band;

\echo '== Coverage'
WITH
params AS (
    SELECT :'cohort_start'::timestamptz AS cohort_start,
           :'cohort_end'::timestamptz AS cohort_end,
           :'as_of'::timestamptz AS as_of
),
cohort AS (
    SELECT fr.*, st.session_id AS task_session_id,
           CASE WHEN st.settled_at < p.as_of THEN st.settled_at END AS settled_at,
           CASE WHEN w.updated_at < p.as_of AND w.created_at < p.as_of
                     AND jsonb_typeof(w.labels) = 'array'
                     AND NOT EXISTS (
                         SELECT 1 FROM jsonb_array_elements(
                             CASE WHEN jsonb_typeof(w.labels) = 'array'
                                  THEN w.labels ELSE '[]'::jsonb END
                         ) label WHERE jsonb_typeof(label) <> 'string'
                     ) THEN w.labels END AS labels
    FROM swarm.factory_receipt fr
    CROSS JOIN params p
    LEFT JOIN swarm.swarm_task st ON st.id = fr.task_id AND st.created_at < p.as_of
    LEFT JOIN swarm.work_item w ON w.id = fr.work_item_id
    WHERE fr.task_id IS NOT NULL
      AND fr.created_at >= p.cohort_start AND fr.created_at < p.cohort_end
      AND fr.created_at < p.as_of
),
runs AS (
    SELECT r.id, r.task_id, r.node_key, r.attempt, r.dispatch_key, r.session_id,
           r.created_at,
           CASE WHEN r.finished_at < p.as_of THEN r.finished_at END AS finished_at,
           CASE WHEN r.finished_at >= p.as_of THEN 'pending' ELSE r.status END AS status,
           CASE WHEN starts_with(r.node_key, 'conductor_funding') THEN 'funding'
                ELSE split_part(r.node_key, '_', 1) END AS role,
           COALESCE(NULLIF(r.model, ''), NULLIF(j.pin ->> 'model', ''), 'unknown') AS model,
           (NULLIF(j.pin ->> 'escalated_from', '') IS NOT NULL) AS pool_escalated,
           j.outcome
    FROM swarm.swarm_node_run r
    JOIN cohort c ON c.task_id = r.task_id
    CROSS JOIN params p
    CROSS JOIN LATERAL (
        SELECT CASE WHEN pg_input_is_valid(r.pin_json, 'jsonb')
                    THEN r.pin_json::jsonb ELSE '{}'::jsonb END AS pin,
               CASE WHEN pg_input_is_valid(r.outcome_json, 'jsonb')
                    THEN r.outcome_json::jsonb ELSE '{}'::jsonb END AS outcome
    ) j
    WHERE r.created_at < p.as_of
),
starts AS (
    SELECT s.*, COALESCE(r.role, 'unassigned') AS role,
           CASE WHEN s.updated_at < p.as_of
                     AND s.status IN ('succeeded', 'failed', 'cancelled')
                THEN COALESCE(s.cost_usd, 0) ELSE 0 END AS settled_usd,
           CASE WHEN s.status IN ('reserved', 'uncertain')
                THEN GREATEST(s.max_cost_usd, COALESCE(s.cost_usd, 0))
                WHEN s.updated_at >= p.as_of
                THEN s.max_cost_usd
                WHEN s.updated_at < p.as_of
                     AND s.status IN ('succeeded', 'failed', 'cancelled')
                     AND s.cost_usd IS NULL
                     AND COALESCE(s.accounting_basis, '') NOT IN (
                         'no_model_post', 'capacity_denied', 'no_session_created')
                THEN s.max_cost_usd ELSE 0 END AS exposure_usd,
           (s.updated_at >= p.as_of OR s.status IN ('reserved', 'uncertain')) AS unsettled,
           (s.updated_at < p.as_of AND s.status IN ('succeeded', 'failed', 'cancelled')
                AND s.cost_usd IS NULL
                AND COALESCE(s.accounting_basis, '') NOT IN (
                    'no_model_post', 'capacity_denied', 'no_session_created')
                ) AS missing_settled_cost
    FROM swarm.factory_start s
    JOIN cohort c ON c.task_id = s.task_id
    CROSS JOIN params p
    LEFT JOIN runs r ON r.task_id = s.task_id AND r.dispatch_key = s.start_key
    WHERE s.created_at < p.as_of
),
session_links AS (
    -- UNION deduplicates a session reached through several ownership paths.
    SELECT task_id, session_id FROM runs WHERE session_id IS NOT NULL
    UNION
    SELECT task_id, session_id FROM starts WHERE session_id IS NOT NULL
    UNION
    SELECT task_id, task_session_id FROM cohort WHERE task_session_id IS NOT NULL
    UNION
    SELECT c.task_id, s.id
    FROM cohort c
    JOIN agent_sessions.agent_sessions s
      ON split_part(s.local_session_id, ':', 1) = 'factory'
     AND split_part(s.local_session_id, ':', 2) = c.task_id
    CROSS JOIN params p
    WHERE s.created_at < p.as_of
),
session_owners AS (
    -- One contribution owner per task/session, earliest run wins reused sessions.
    SELECT l.task_id, l.session_id,
           COALESCE(r.role, s.role,
                    CASE WHEN c.task_session_id = l.session_id THEN 'conductor' END,
                    'unassigned') AS role,
           COALESCE(r.model, s.model, 'unknown') AS model
    FROM session_links l
    JOIN cohort c ON c.task_id = l.task_id
    JOIN agent_sessions.agent_sessions a ON a.id = l.session_id
    CROSS JOIN params p
    LEFT JOIN LATERAL (
        SELECT role, model FROM runs r
        WHERE r.task_id = l.task_id AND r.session_id = l.session_id
        ORDER BY r.created_at, r.id LIMIT 1
    ) r ON true
    LEFT JOIN LATERAL (
        SELECT role, model FROM starts s
        WHERE s.task_id = l.task_id AND s.session_id = l.session_id
        ORDER BY s.created_at, s.id LIMIT 1
    ) s ON true
    WHERE a.created_at < p.as_of
),
turns AS (
    -- Unique turn IDs and one owner per task/session prevent duplicate paths.
    SELECT t.id, o.task_id, o.role,
           COALESCE(NULLIF(t.model, ''), o.model) AS model, t.list_cost_usd
    FROM session_owners o
    JOIN agent_sessions.agent_turns t ON t.session_id = o.session_id
    CROSS JOIN params p
    WHERE t.created_at < p.as_of
),
task_cost AS (
    SELECT task_id, COALESCE(SUM(list_cost_usd), 0) AS list_usd,
           COUNT(*) FILTER (WHERE list_cost_usd IS NULL) AS unpriced_turns,
           COUNT(*) AS turn_count
    FROM turns GROUP BY task_id
),
task_ledger AS (
    SELECT task_id, SUM(settled_usd) AS settled_usd, SUM(exposure_usd) AS exposure_usd,
           COUNT(*) FILTER (WHERE unsettled) AS unsettled_reservations,
           COUNT(*) FILTER (WHERE missing_settled_cost) AS missing_settled_costs
    FROM starts GROUP BY task_id
),
audits AS (
    SELECT a.id, a.task_id, a.action, a.created_at,
           CASE WHEN pg_input_is_valid(a.detail_json, 'jsonb')
                THEN a.detail_json::jsonb ELSE '{}'::jsonb END AS detail
    FROM swarm.factory_audit a
    JOIN cohort c ON c.task_id = a.task_id
    CROSS JOIN params p
    WHERE a.created_at < p.as_of
),
merge_audits AS (
    -- Collapse every audit kind before task joins. Earliest merge defines delivery.
    SELECT task_id, MIN(created_at) AS merged_at,
           (array_agg(detail ORDER BY created_at, id))[1] AS detail
    FROM audits WHERE action = 'merged' GROUP BY task_id
),
delivery_refs AS (
    SELECT task_id,
           substring(detail #>> '{evidence,pr_url}'
                     FROM '^https://github[.]com/jomcgi-org/homelab/pull/([0-9]+)/?$') AS pr
    FROM audits WHERE action IN ('finish_task', 'delivery_ready')
    UNION
    SELECT task_id, COALESCE(outcome #>> '{value,pr_number}', outcome ->> 'pr_number')
    FROM runs
    CROSS JOIN params p
    WHERE finished_at < p.as_of
),
historical_merges AS (
    SELECT d.task_id, MIN(m.merged_at) AS merged_at,
           (array_agg(m.number ORDER BY m.merged_at, m.number))[1]::text AS pr
    FROM delivery_refs d
    JOIN cohort c ON c.task_id = d.task_id AND c.repo = 'jomcgi-org/homelab'
    JOIN observability.merged_prs m ON m.number::text = d.pr
    CROSS JOIN params p
    WHERE m.merged_at < p.as_of
    GROUP BY d.task_id
),
merges AS (
    SELECT c.task_id, COALESCE(a.merged_at, h.merged_at) AS merged_at,
           CASE WHEN a.task_id IS NOT NULL THEN a.detail ->> 'pr_number' ELSE h.pr END AS pr,
           a.detail ->> 'merge_commit_sha' AS merge_sha,
           (a.task_id IS NULL AND h.task_id IS NOT NULL) AS historical_unjudged
    FROM cohort c
    LEFT JOIN merge_audits a ON a.task_id = c.task_id
    LEFT JOIN historical_merges h ON h.task_id = c.task_id
),
ci_audits AS (
    SELECT a.task_id,
           (array_agg(a.detail ->> 'conclusion' ORDER BY a.created_at, a.id))[1] AS conclusion
    FROM audits a
    JOIN merges m ON m.task_id = a.task_id
                 AND (m.merged_at IS NULL OR a.detail ->> 'pr_number' = m.pr)
    WHERE a.action = 'merge_ci'
      AND (m.merged_at IS NULL OR a.created_at >= m.merged_at)
      AND NULLIF(a.detail ->> 'pr_number', '') IS NOT NULL
      AND a.detail ->> 'head_sha' ~ '^[0-9a-f]{40}$'
    GROUP BY a.task_id
),
revert_audits AS (
    SELECT task_id, true AS reverted FROM audits
    WHERE action = 'reverted' GROUP BY task_id
),
window_audits AS (
    SELECT a.task_id, true AS window_closed
    FROM audits a
    JOIN merges m ON m.task_id = a.task_id
                 AND a.detail ->> 'pr_number' = m.pr
                 AND a.detail ->> 'merge_commit_sha' = m.merge_sha
    CROSS JOIN params p
    WHERE a.action = 'revert_window_closed'
      AND NULLIF(m.merge_sha, '') IS NOT NULL
      AND a.created_at >= m.merged_at + interval '7 days'
      AND m.merged_at + interval '7 days' <= p.as_of
    GROUP BY a.task_id
),
initiators AS (
    SELECT DISTINCT ON (task_id) task_id, model
    FROM runs WHERE role = 'implement'
    ORDER BY task_id, created_at, id
),
run_evidence AS (
    SELECT task_id, COUNT(DISTINCT node_key) FILTER (WHERE starts_with(node_key, 'correct_'))
               AS fixup_rounds,
           BOOL_OR(status = 'escalated') AS escalated,
           BOOL_OR(pool_escalated) AS pool_escalated,
           BOOL_OR(model = 'unknown') AS unknown_model
    FROM runs GROUP BY task_id
),
task_evidence AS (
    SELECT c.*, m.merged_at, m.pr, m.historical_unjudged,
           COALESCE(i.model, 'none') AS initiating_model,
           ci.conclusion AS ci_conclusion, COALESCE(rv.reverted, false) AS reverted,
           COALESCE(w.window_closed, false) AS window_closed,
           COALESCE(tc.list_usd, 0) AS list_usd,
           COALESCE(tc.unpriced_turns, 0) AS unpriced_turns,
           COALESCE(tc.turn_count, 0) AS turn_count,
           COALESCE(tl.settled_usd, 0) AS settled_usd,
           COALESCE(tl.exposure_usd, 0) AS exposure_usd,
           COALESCE(tl.unsettled_reservations, 0) AS unsettled_reservations,
           COALESCE(tl.missing_settled_costs, 0) AS missing_settled_costs,
           COALESCE(re.fixup_rounds, 0) AS fixup_rounds,
           (COALESCE(re.escalated, false)
            OR COALESCE(re.pool_escalated, false)
            OR (c.updated_at < p.as_of AND pg_input_is_valid(c.escalation_json, 'jsonb')
                AND CASE WHEN pg_input_is_valid(c.escalation_json, 'jsonb')
                         THEN c.escalation_json::jsonb ELSE '{}'::jsonb END
                    NOT IN ('{}'::jsonb, 'null'::jsonb, '[]'::jsonb, '""'::jsonb))) AS escalated,
           (c.updated_at >= p.as_of OR COALESCE(re.unknown_model, false)) AS escalation_unknown,
           mp.additions + mp.deletions AS changed_lines, mp.changed_files,
           EXTRACT(EPOCH FROM COALESCE(m.merged_at, c.settled_at) - c.created_at) / 3600
               AS elapsed_hours
    FROM cohort c CROSS JOIN params p
    LEFT JOIN merges m ON m.task_id = c.task_id
    LEFT JOIN initiators i ON i.task_id = c.task_id
    LEFT JOIN ci_audits ci ON ci.task_id = c.task_id
    LEFT JOIN revert_audits rv ON rv.task_id = c.task_id
    LEFT JOIN window_audits w ON w.task_id = c.task_id
    LEFT JOIN task_cost tc ON tc.task_id = c.task_id
    LEFT JOIN task_ledger tl ON tl.task_id = c.task_id
    LEFT JOIN run_evidence re ON re.task_id = c.task_id
    LEFT JOIN observability.merged_prs mp ON mp.number::text = m.pr AND mp.merged_at < p.as_of
),
tasks AS (
    SELECT e.*,
           CASE WHEN reverted THEN 'reverted'
                WHEN ci_conclusion = 'failure' THEN 'ci_failed'
                WHEN historical_unjudged THEN 'unknown'
                WHEN merged_at IS NOT NULL AND ci_conclusion = 'success' AND window_closed
                     AND merged_at + interval '7 days' <= p.as_of THEN 'positive'
                WHEN merged_at IS NOT NULL AND ci_conclusion = 'success' THEN 'pending_maturity'
                WHEN merged_at IS NOT NULL THEN 'unknown'
                WHEN settled_at IS NOT NULL THEN 'failed'
                ELSE 'censored' END AS outcome_state
    FROM task_evidence e CROSS JOIN params p
)
SELECT COUNT(*) AS tasks,
       COUNT(*) FILTER (WHERE merged_at IS NOT NULL
                        AND COALESCE(ci_conclusion, 'unknown') NOT IN ('success', 'failure'))
           AS unknown_ci_evidence,
       COUNT(*) FILTER (WHERE historical_unjudged) AS historical_unjudged_merges,
       COUNT(*) FILTER (WHERE outcome_state = 'pending_maturity') AS pending_maturity,
       SUM(unpriced_turns) AS unpriced_turns, SUM(unsettled_reservations) AS unsettled_reservations,
       SUM(exposure_usd) AS exposure_usd, SUM(missing_settled_costs) AS missing_settled_costs,
       COUNT(*) FILTER (WHERE outcome_state = 'censored') AS censored_tasks,
       COUNT(*) FILTER (WHERE merged_at IS NULL OR changed_lines IS NULL) AS missing_size_metadata,
       COUNT(*) FILTER (WHERE merged_at IS NULL OR changed_files IS NULL) AS missing_file_metadata,
       COUNT(*) FILTER (WHERE merged_at IS NULL OR labels IS NULL) AS missing_label_metadata,
       COUNT(*) AS first_pass_ci_unknown_count,
       COUNT(*) FILTER (WHERE NOT escalated AND escalation_unknown) AS escalation_unknown_count,
       COUNT(*) FILTER (WHERE initiating_model = 'unknown') AS missing_initiating_model
FROM tasks;
