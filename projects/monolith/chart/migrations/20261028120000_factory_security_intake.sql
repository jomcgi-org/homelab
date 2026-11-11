-- Admit agent-ready security findings under existing eligibility fences (#7372).
-- Only the two known legacy global lists change. Repository overrides, custom
-- membership and every unrelated policy field retain their stored values.
-- The row lock, policy update and matching audit belong to one statement.
WITH before AS (
    SELECT id, policy_json::jsonb AS policy
    FROM swarm.factory_control
    WHERE id = 'factory'
      AND policy_json::jsonb ->> 'repo' = 'jomcgi-org/homelab'
      AND policy_json::jsonb #> '{intake,exclude_labels}' IN (
          '["needs-human", "wontfix", "security-finding"]'::jsonb,
          '["needs-human", "security-finding", "wontfix"]'::jsonb
      )
    FOR UPDATE
),
after AS (
    SELECT id, policy AS before_policy,
        jsonb_set(
            policy, '{intake,exclude_labels}',
            (policy #> '{intake,exclude_labels}') - 'security-finding',
            false
        ) AS after_policy
    FROM before
),
updated AS (
    UPDATE swarm.factory_control AS control
    SET policy_json = after.after_policy::text,
        version = control.version + 1,
        actor = 'migration:20261028120000_factory_security_intake',
        updated_at = now()
    FROM after
    WHERE control.id = after.id
    RETURNING control.version
)
INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json)
SELECT
    'migration:20261028120000_factory_security_intake',
    'policy_migrated',
    NULL,
    jsonb_build_object(
        'migration', '20261028120000_factory_security_intake',
        'version', updated.version,
        'before', jsonb_build_object('intake', after.before_policy -> 'intake'),
        'after', jsonb_build_object('intake', after.after_policy -> 'intake')
    )::text
FROM after
CROSS JOIN updated;
