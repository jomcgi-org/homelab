-- One-shot GitOps policy change: generation 14, workers lead with Sol.
--
-- generation 13 -> 14. Intake skips an issue that already holds a receipt of
-- the same class at the current generation, so the agent-ready issues that
-- failed during the 2026-09-19..23 jam can never re-admit at 13. Active and
-- landing work keeps running under its own pinned policy_json; generation
-- retirement only settles old queued receipts and unresolved cards.
--
-- worker_model spark -> sol, and the worker, implement and refine pools lead
-- with sol, keeping the previous members behind it in their previous order.
-- Codex quota was unused (5% of the week on 2026-09-30) while Muse and Claude
-- carried the work; Opus still plans and reviews. A pool that does not name
-- sol is left as it is.
--
-- issue_numbers drops closed #4361; #6288 stays so the list is non-empty.
--
-- The guard makes this a no-op on a re-run and on any policy that is not the
-- exact generation-13 spark-worker policy it was written against.
--
-- The UPDATE and its audit row are one statement: the audit row exists only
-- when the guarded UPDATE changed the control row.
WITH before AS (
    SELECT id, policy_json::jsonb AS policy
    FROM swarm.factory_control
    WHERE id = 'factory'
      AND (policy_json::jsonb ->> 'generation')::int = 13
      AND policy_json::jsonb ->> 'worker_model' = 'spark'
      AND policy_json::jsonb -> 'allowed_models' ? 'sol'
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
                        WHEN role IN ('worker', 'implement', 'refine')
                            AND jsonb_typeof(pool) = 'array'
                            AND pool ? 'sol'
                        THEN '["sol"]'::jsonb || (
                            SELECT COALESCE(jsonb_agg(model ORDER BY ordinality), '[]'::jsonb)
                            FROM jsonb_array_elements(pool)
                                WITH ORDINALITY AS member(model, ordinality)
                            WHERE model <> '"sol"'::jsonb
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
                'generation', 14,
                'worker_model', 'sol',
                'issue_numbers', COALESCE(
                    (
                        SELECT jsonb_agg(issue ORDER BY issue)
                        FROM jsonb_array_elements(policy -> 'issue_numbers') AS entry(issue)
                        WHERE issue <> '4361'::jsonb
                    ),
                    '[6288]'::jsonb
                )
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
        actor = 'migration:20260930210000_factory_policy_gen14_sol',
        updated_at = now()
    FROM after
    WHERE control.id = after.id
    RETURNING control.version
)
INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json)
SELECT
    'migration:20260930210000_factory_policy_gen14_sol',
    'policy_migrated',
    NULL,
    jsonb_build_object(
        'migration', '20260930210000_factory_policy_gen14_sol',
        'version', updated.version,
        'before', jsonb_build_object(
            'generation', after.before_policy -> 'generation',
            'worker_model', after.before_policy -> 'worker_model',
            'issue_numbers', after.before_policy -> 'issue_numbers',
            'model_pools', after.before_policy -> 'model_pools'
        ),
        'after', jsonb_build_object(
            'generation', after.after_policy -> 'generation',
            'worker_model', after.after_policy -> 'worker_model',
            'issue_numbers', after.after_policy -> 'issue_numbers',
            'model_pools', after.after_policy -> 'model_pools'
        )
    )::text
FROM after
CROSS JOIN updated;
