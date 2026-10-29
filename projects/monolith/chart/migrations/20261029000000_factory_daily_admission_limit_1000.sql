-- Joe requested 1000 daily factory admissions in issue #7377 on 2026-10-28.
-- Change only the current homelab intake cap. Preserve concurrency, budgets,
-- retries, ownership controls and pinned receipts. Leave other operator caps.
WITH before AS (
    SELECT id, policy_json::jsonb AS policy
    FROM swarm.factory_control
    WHERE id = 'factory'
      AND policy_json::jsonb ->> 'repo' = 'jomcgi-org/homelab'
      AND policy_json::jsonb #> '{intake,max_per_day}' = '500'::jsonb
    FOR UPDATE
),
updated AS (
    UPDATE swarm.factory_control AS control
    SET policy_json = jsonb_set(before.policy, '{intake,max_per_day}', '1000'::jsonb, false)::text,
        version = control.version + 1,
        actor = 'migration:20261029000000_factory_daily_admission_limit_1000',
        updated_at = now()
    FROM before
    WHERE control.id = before.id
    RETURNING control.version
)
INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json)
SELECT
    'migration:20261029000000_factory_daily_admission_limit_1000',
    'policy_migrated',
    NULL,
    jsonb_build_object(
        'migration', '20261029000000_factory_daily_admission_limit_1000',
        'version', updated.version,
        'before', jsonb_build_object('intake', jsonb_build_object('max_per_day', 500)),
        'after', jsonb_build_object('intake', jsonb_build_object('max_per_day', 1000))
    )::text
FROM updated;
