-- Use Terra/xhigh for new ordinary reviews after the verified runtime rollout.
-- Keep generation, admission state, existing receipt pins and all other policy.
WITH before AS (
    SELECT id, policy_json::jsonb AS policy
    FROM swarm.factory_control
    WHERE id = 'factory'
      AND policy_json::jsonb ->> 'repo' = 'jomcgi-org/homelab'
      AND policy_json::jsonb ->> 'generation' = '14'
      AND policy_json::jsonb ->> 'conductor_model' = 'astra'
      AND policy_json::jsonb ->> 'worker_model' = 'astra'
      AND policy_json::jsonb ->> 'reviewer_model' = 'opus'
      AND policy_json::jsonb -> 'allowed_models' ?& ARRAY['astra', 'sol', 'spark', 'sonnet', 'opus']
      AND CASE WHEN jsonb_typeof(policy_json::jsonb -> 'allowed_models') = 'array' THEN
          jsonb_array_length(policy_json::jsonb -> 'allowed_models') <= 16
          AND (
              jsonb_array_length(policy_json::jsonb -> 'allowed_models') < 16
              OR policy_json::jsonb -> 'allowed_models' ? 'terra'
          )
          ELSE false END
      AND policy_json::jsonb #> '{model_pools,reviewer}' = '["opus","astra","sol"]'::jsonb
      AND policy_json::jsonb #>> '{role_effort,reviewer}' = 'xhigh'
    FOR UPDATE
), after AS (
    SELECT id, policy AS before_policy,
        jsonb_set(
            jsonb_set(
                jsonb_set(
                    jsonb_set(policy, '{reviewer_model}', '"terra"'::jsonb),
                    '{allowed_models}',
                    CASE WHEN policy -> 'allowed_models' ? 'terra'
                        THEN policy -> 'allowed_models'
                        ELSE (policy -> 'allowed_models') || '["terra"]'::jsonb
                    END
                ),
                '{model_pools,reviewer}', '["terra","opus","astra","sol"]'::jsonb
            ),
            '{role_effort,reviewer}', '"xhigh"'::jsonb
        ) AS after_policy
    FROM before
), updated AS (
    UPDATE swarm.factory_control AS control
    SET policy_json = after.after_policy::text,
        version = control.version + 1,
        actor = 'migration:20261106090000_factory_terra_reviews',
        updated_at = now()
    FROM after
    WHERE control.id = after.id
    RETURNING control.version
)
INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json)
SELECT 'migration:20261106090000_factory_terra_reviews',
    'policy_migrated', NULL,
    jsonb_build_object(
        'migration', '20261106090000_factory_terra_reviews',
        'version', updated.version,
        'before', jsonb_build_object(
            'reviewer_model', after.before_policy -> 'reviewer_model',
            'allowed_models', after.before_policy -> 'allowed_models',
            'model_pools', after.before_policy -> 'model_pools',
            'role_effort', after.before_policy -> 'role_effort'
        ),
        'after', jsonb_build_object(
            'reviewer_model', after.after_policy -> 'reviewer_model',
            'allowed_models', after.after_policy -> 'allowed_models',
            'model_pools', after.after_policy -> 'model_pools',
            'role_effort', after.after_policy -> 'role_effort'
        )
    )::text
FROM after CROSS JOIN updated;
