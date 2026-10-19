-- Prefer Sol for new, unpinned implementation nodes only. Preserve all other
-- routing, effort, limits, and receipt snapshots from the verified policy.
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
      AND policy_json::jsonb #> '{model_pools,implement}' = '["astra","sol","spark","sonnet"]'::jsonb
    FOR UPDATE
), after AS (
    SELECT id, policy AS before_policy,
        jsonb_set(policy, '{model_pools,implement}', '["sol","astra","spark","sonnet"]'::jsonb) AS after_policy
    FROM before
), updated AS (
    UPDATE swarm.factory_control AS control
    SET policy_json = after.after_policy::text,
        version = control.version + 1,
        actor = 'migration:20261019093000_factory_implement_sol_first',
        updated_at = now()
    FROM after
    WHERE control.id = after.id
    RETURNING control.version
)
INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json)
SELECT 'migration:20261019093000_factory_implement_sol_first',
    'policy_migrated', NULL,
    jsonb_build_object(
        'migration', '20261019093000_factory_implement_sol_first',
        'version', updated.version,
        'before', jsonb_build_object('model_pools', after.before_policy -> 'model_pools'),
        'after', jsonb_build_object('model_pools', after.after_policy -> 'model_pools')
    )::text
FROM after CROSS JOIN updated;
