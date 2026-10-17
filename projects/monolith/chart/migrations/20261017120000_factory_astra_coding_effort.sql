-- Per-node reasoning is optional for historical plans and callers.
ALTER TABLE swarm.swarm_plan_node ADD COLUMN IF NOT EXISTS effort text;

-- Promote Astra for new coding admissions from the verified generation-14
-- planner policy. Existing receipts, bounds, and unrelated roles stay pinned.
WITH before AS (
    SELECT id, policy_json::jsonb AS policy
    FROM swarm.factory_control
    WHERE id = 'factory'
      AND policy_json::jsonb ->> 'repo' = 'jomcgi-org/homelab'
      AND policy_json::jsonb ->> 'generation' = '14'
      AND policy_json::jsonb ->> 'conductor_model' = 'astra'
      AND policy_json::jsonb ->> 'worker_model' = 'sol'
      AND policy_json::jsonb ->> 'reviewer_model' = 'opus'
      AND policy_json::jsonb -> 'allowed_models' ?& ARRAY['astra', 'sol', 'opus']
      AND policy_json::jsonb #>> '{model_pools,worker,0}' = 'sol'
      AND policy_json::jsonb #>> '{model_pools,implement,0}' = 'sol'
      AND policy_json::jsonb #> '{model_pools,worker}' ? 'astra'
      AND policy_json::jsonb #> '{model_pools,implement}' ? 'astra'
    FOR UPDATE
), after AS (
    SELECT id, policy AS before_policy,
        policy || jsonb_build_object(
            'worker_model', 'astra',
            'model_pools', (policy -> 'model_pools') || (
                SELECT jsonb_object_agg(role, '["astra"]'::jsonb || (
                    SELECT COALESCE(jsonb_agg(model ORDER BY ordinality), '[]'::jsonb)
                    FROM jsonb_array_elements(policy #> ARRAY['model_pools', role])
                        WITH ORDINALITY AS member(model, ordinality)
                    WHERE model <> '"astra"'::jsonb
                ))
                FROM (VALUES ('worker'), ('implement')) AS roles(role)
            ),
            'role_effort', COALESCE(policy -> 'role_effort', '{}'::jsonb) ||
                '{"planner":"xhigh","reviewer":"xhigh","implement":"medium","integrate":"xhigh","correct":"xhigh"}'::jsonb
        ) AS after_policy
    FROM before
), updated AS (
    UPDATE swarm.factory_control AS control
    SET policy_json = after.after_policy::text,
        version = control.version + 1,
        actor = 'migration:20261017120000_factory_astra_coding_effort',
        updated_at = now()
    FROM after
    WHERE control.id = after.id
    RETURNING control.version
)
INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json)
SELECT 'migration:20261017120000_factory_astra_coding_effort',
    'policy_migrated', NULL,
    jsonb_build_object(
        'migration', '20261017120000_factory_astra_coding_effort',
        'version', updated.version,
        'before', jsonb_build_object(
            'worker_model', after.before_policy -> 'worker_model',
            'model_pools', after.before_policy -> 'model_pools',
            'role_effort', after.before_policy -> 'role_effort'
        ),
        'after', jsonb_build_object(
            'worker_model', after.after_policy -> 'worker_model',
            'model_pools', after.after_policy -> 'model_pools',
            'role_effort', after.after_policy -> 'role_effort'
        )
    )::text
FROM after CROSS JOIN updated;
