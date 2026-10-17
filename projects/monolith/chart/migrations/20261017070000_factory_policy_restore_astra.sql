-- Restore Astra planning after the temporary September quota-saving policy.
-- Joe confirmed both Codex grants have headroom (#6983). Sol still leads
-- implementation and Opus still leads independent review. Only new admissions
-- see this policy; pinned receipts, their authority and all budgets stay put.
-- The guard leaves an operator-changed role/generation or a full pool alone.
WITH before AS (
    SELECT id, policy_json::jsonb AS policy
    FROM swarm.factory_control
    WHERE id = 'factory'
      AND policy_json::jsonb ->> 'repo' = 'jomcgi-org/homelab'
      AND policy_json::jsonb ->> 'generation' = '14'
      AND policy_json::jsonb ->> 'conductor_model' = 'opus'
      AND policy_json::jsonb ->> 'worker_model' = 'sol'
      AND policy_json::jsonb ->> 'reviewer_model' = 'opus'
      AND policy_json::jsonb -> 'allowed_models' ?& ARRAY['opus', 'sol']
      AND NOT (policy_json::jsonb -> 'allowed_models' ? 'astra')
      AND jsonb_array_length(policy_json::jsonb -> 'allowed_models') < 16
      AND jsonb_typeof(policy_json::jsonb -> 'model_pools') = 'object'
      AND NOT EXISTS (
          SELECT 1
          FROM jsonb_each(policy_json::jsonb -> 'model_pools') AS pool(role, models)
          WHERE role IN ('conductor', 'worker', 'implement', 'reviewer', 'refine')
            AND jsonb_array_length(models) >= 8
      )
    FOR UPDATE
),
pools AS (
    SELECT
        id,
        policy,
        (policy -> 'model_pools') || jsonb_object_agg(
            role,
            CASE
                WHEN role IN ('conductor', 'refine')
                    THEN '["astra"]'::jsonb || models
                ELSE models || '["astra"]'::jsonb
            END
        ) AS model_pools
    FROM before
    CROSS JOIN LATERAL (
        VALUES
            ('conductor', COALESCE(policy #> '{model_pools,conductor}', '["opus","sol"]'::jsonb)),
            ('worker', COALESCE(policy #> '{model_pools,worker}', '["sol"]'::jsonb)),
            ('implement', COALESCE(policy #> '{model_pools,implement}', policy #> '{model_pools,worker}', '["sol"]'::jsonb)),
            ('reviewer', COALESCE(policy #> '{model_pools,reviewer}', '["opus"]'::jsonb)),
            ('refine', COALESCE(
                policy #> '{model_pools,refine}',
                CASE WHEN policy -> 'allowed_models' ? 'spark'
                    THEN '["spark","sol"]'::jsonb
                    ELSE '["sol"]'::jsonb
                END
            ))
    ) AS roles(role, models)
    GROUP BY id, policy
),
after AS (
    SELECT
        id,
        policy AS before_policy,
        policy || jsonb_build_object(
            'conductor_model', 'astra',
            'allowed_models', (
                SELECT jsonb_agg(model ORDER BY model COLLATE "C")
                FROM jsonb_array_elements_text(
                    (policy -> 'allowed_models') || '["astra"]'::jsonb
                ) AS member(model)
            ),
            'model_pools', model_pools
        ) AS after_policy
    FROM pools
),
updated AS (
    UPDATE swarm.factory_control AS control
    SET policy_json = after.after_policy::text,
        version = control.version + 1,
        actor = 'migration:20261017070000_factory_policy_restore_astra',
        updated_at = now()
    FROM after
    WHERE control.id = after.id
    RETURNING control.version
)
INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json)
SELECT
    'migration:20261017070000_factory_policy_restore_astra',
    'policy_migrated',
    NULL,
    jsonb_build_object(
        'migration', '20261017070000_factory_policy_restore_astra',
        'version', updated.version,
        'before', jsonb_build_object(
            'conductor_model', after.before_policy -> 'conductor_model',
            'allowed_models', after.before_policy -> 'allowed_models',
            'model_pools', after.before_policy -> 'model_pools'
        ),
        'after', jsonb_build_object(
            'conductor_model', after.after_policy -> 'conductor_model',
            'allowed_models', after.after_policy -> 'allowed_models',
            'model_pools', after.after_policy -> 'model_pools'
        )
    )::text
FROM after
CROSS JOIN updated;
