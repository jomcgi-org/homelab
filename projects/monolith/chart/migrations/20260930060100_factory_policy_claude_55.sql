-- One-shot GitOps policy change for the Claude 5.5 profile (#6461), staged.
--
-- allowed_models gains sonnet (pinned to claude-sonnet-5-5 in the guest).
-- An existing worker or implement pool that routes spark gains sonnet right
-- after spark, so spark still leads and sonnet only takes work spark cannot;
-- worker_model is NOT flipped. A pool without spark, one that already names
-- sonnet, or one already at the eight-model cap is left as it is, and an
-- absent model_pools stays absent. task_budget_usd rises to at least 50,
-- because xhigh planner and review turns cost more and recent tasks were
-- refused envelope_exceeded by under $5 at $36. Every other key, generation
-- included, is left as it is. Active tasks keep their own pinned
-- factory_receipt.policy_json, so only future admissions see the change.
--
-- The guard makes this a no-op on a re-run, on any policy that already
-- allows sonnet and budgets at least $50, and on a control row that holds no
-- configured policy yet (a fresh database).
--
-- allowed_models is deduplicated and ordered with COLLATE "C", which is the
-- codepoint order of validate_policy's sorted(set(...)). policy_json is
-- written back as jsonb text; every reader compares json.loads dicts.
--
-- The UPDATE and its audit row are one statement: the audit row exists only
-- when the guarded UPDATE changed the control row.
WITH before AS (
    SELECT id, policy_json::jsonb AS policy
    FROM swarm.factory_control
    WHERE id = 'factory'
      AND jsonb_typeof(policy_json::jsonb -> 'allowed_models') = 'array'
      AND (
          NOT policy_json::jsonb -> 'allowed_models' ? 'sonnet'
          OR (policy_json::jsonb ->> 'task_budget_usd')::numeric < 50
      )
    FOR UPDATE
),
pools AS (
    SELECT
        id,
        policy,
        CASE
            WHEN jsonb_typeof(policy -> 'model_pools') = 'object' THEN (
                SELECT jsonb_object_agg(
                    role,
                    CASE
                        WHEN role IN ('worker', 'implement')
                            AND jsonb_typeof(pool) = 'array'
                            AND pool ? 'spark'
                            AND NOT pool ? 'sonnet'
                            AND jsonb_array_length(pool) < 8
                        THEN (
                            SELECT jsonb_agg(model ORDER BY position)
                            FROM (
                                SELECT model, ordinality::numeric AS position
                                FROM jsonb_array_elements(pool)
                                    WITH ORDINALITY AS member(model, ordinality)
                                UNION ALL
                                SELECT '"sonnet"'::jsonb, ordinality + 0.5
                                FROM jsonb_array_elements_text(pool)
                                    WITH ORDINALITY AS member(model, ordinality)
                                WHERE model = 'spark'
                            ) AS placed
                        )
                        ELSE pool
                    END
                )
                FROM jsonb_each(policy -> 'model_pools') AS entry(role, pool)
            )
            ELSE NULL
        END AS model_pools
    FROM before
),
after AS (
    SELECT
        id,
        policy AS before_policy,
        policy
            || jsonb_build_object(
                'allowed_models', (
                    SELECT jsonb_agg(model ORDER BY model COLLATE "C")
                    FROM (
                        SELECT DISTINCT model
                        FROM (
                            SELECT jsonb_array_elements_text(
                                CASE
                                    WHEN jsonb_typeof(policy -> 'allowed_models') = 'array'
                                        THEN policy -> 'allowed_models'
                                    ELSE '[]'::jsonb
                                END
                            ) AS model
                            UNION ALL
                            SELECT 'sonnet'
                        ) AS merged
                    ) AS distinct_models
                ),
                'task_budget_usd', GREATEST((policy ->> 'task_budget_usd')::numeric, 50)
            )
            || CASE
                WHEN model_pools IS NULL THEN '{}'::jsonb
                ELSE jsonb_build_object('model_pools', model_pools)
            END AS after_policy
    FROM pools
),
updated AS (
    UPDATE swarm.factory_control AS control
    SET policy_json = after.after_policy::text,
        version = control.version + 1,
        actor = 'migration:20260930060100_factory_policy_claude_55',
        updated_at = now()
    FROM after
    WHERE control.id = after.id
    RETURNING control.version
)
INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json)
SELECT
    'migration:20260930060100_factory_policy_claude_55',
    'policy_migrated',
    NULL,
    jsonb_build_object(
        'migration', '20260930060100_factory_policy_claude_55',
        'version', updated.version,
        'before', jsonb_build_object(
            'allowed_models', after.before_policy -> 'allowed_models',
            'model_pools', after.before_policy -> 'model_pools',
            'task_budget_usd', after.before_policy -> 'task_budget_usd'
        ),
        'after', jsonb_build_object(
            'allowed_models', after.after_policy -> 'allowed_models',
            'model_pools', after.after_policy -> 'model_pools',
            'task_budget_usd', after.after_policy -> 'task_budget_usd'
        )
    )::text
FROM after
CROSS JOIN updated;
