-- Read-only lifecycle export for factory/baseline.py.
-- Required psql variables are window_start and window_end, both RFC3339 UTC.
-- The query emits one compact JSON object per line. It does not create tables.
WITH cohort AS (
    SELECT DISTINCT r.task_id
    FROM swarm.factory_receipt AS r
    WHERE r.task_id IS NOT NULL
      AND r.created_at >= :'window_start'::timestamptz
      AND r.created_at < :'window_end'::timestamptz
),
events AS (
    SELECT
        'factory_receipt:' || r.id || ':intake' AS event_id,
        'task_intake' AS event_type,
        r.created_at AS occurred_at,
        r.task_id,
        NULL::text AS change_id,
        r.url AS evidence_url,
        NULL::text AS outcome,
        NULL::double precision AS cost_usd,
        NULL::double precision AS reserved_cost_usd,
        FALSE AS cost_expected,
        NULL::text AS actor
    FROM swarm.factory_receipt AS r
    JOIN cohort AS c USING (task_id)

    UNION ALL

    -- factory_start is the accounting ledger. Do not also sum node-run costs.
    SELECT
        'factory_start:' || s.id AS event_id,
        CASE
            WHEN s.start_key ~* '(review|correct)' THEN 'review_correction'
            ELSE 'agent_attempt_finished'
        END AS event_type,
        s.updated_at AS occurred_at,
        s.task_id,
        NULL::text AS change_id,
        r.url AS evidence_url,
        s.status AS outcome,
        s.cost_usd,
        s.max_cost_usd AS reserved_cost_usd,
        TRUE AS cost_expected,
        NULL::text AS actor
    FROM swarm.factory_start AS s
    JOIN cohort AS c USING (task_id)
    JOIN swarm.factory_receipt AS r USING (task_id)

    UNION ALL

    SELECT
        'factory_audit:' || a.id AS event_id,
        CASE a.action
            WHEN 'merged' THEN 'pr_merged'
            WHEN 'rollout_verified' THEN 'verified_outcome'
        END AS event_type,
        a.created_at AS occurred_at,
        a.task_id,
        CASE
            WHEN a.action = 'merged'
            THEN 'github-pr:' || (a.detail_json::jsonb ->> 'pr_number')
        END AS change_id,
        r.url AS evidence_url,
        NULL::text AS outcome,
        NULL::double precision AS cost_usd,
        NULL::double precision AS reserved_cost_usd,
        FALSE AS cost_expected,
        NULL::text AS actor
    FROM swarm.factory_audit AS a
    JOIN cohort AS c USING (task_id)
    JOIN swarm.factory_receipt AS r USING (task_id)
    WHERE a.action IN ('merged', 'rollout_verified')

    UNION ALL

    SELECT
        'factory_receipt:' || r.id || ':terminal' AS event_id,
        CASE r.state
            WHEN 'cancelled' THEN 'task_cancelled'
            WHEN 'failed' THEN 'task_abandoned'
        END AS event_type,
        r.updated_at AS occurred_at,
        r.task_id,
        NULL::text AS change_id,
        r.url AS evidence_url,
        r.state AS outcome,
        NULL::double precision AS cost_usd,
        NULL::double precision AS reserved_cost_usd,
        FALSE AS cost_expected,
        NULL::text AS actor
    FROM swarm.factory_receipt AS r
    JOIN cohort AS c USING (task_id)
    WHERE r.state IN ('cancelled', 'failed')

    UNION ALL

    SELECT
        'swarm_decision:' || d.id AS event_id,
        CASE
            WHEN d.decided_at IS NULL THEN 'intervention_required'
            ELSE 'operator_intervention'
        END AS event_type,
        COALESCE(d.decided_at, d.requested_at) AS occurred_at,
        t.id AS task_id,
        NULL::text AS change_id,
        r.url AS evidence_url,
        d.decision AS outcome,
        NULL::double precision AS cost_usd,
        NULL::double precision AS reserved_cost_usd,
        FALSE AS cost_expected,
        d.actor_subject AS actor
    FROM swarm.swarm_decision AS d
    JOIN swarm.swarm_task AS t ON t.workflow_id = d.workflow_id
    JOIN cohort AS c ON c.task_id = t.id
    JOIN swarm.factory_receipt AS r ON r.task_id = t.id
    WHERE d.decided_at IS NULL OR d.actor_subject IS NOT NULL
)
SELECT jsonb_strip_nulls(jsonb_build_object(
    'event_id', event_id,
    'event_type', event_type,
    'occurred_at', to_char(occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'revision', 1,
    'task_id', task_id,
    'change_id', change_id,
    'evidence_url', evidence_url,
    'outcome', outcome,
    'cost_usd', cost_usd,
    'reserved_cost_usd', reserved_cost_usd,
    'cost_expected', cost_expected,
    'actor', actor
))::text
FROM events
WHERE event_type IS NOT NULL
ORDER BY occurred_at, event_id;
