-- Joe requested 500 daily factory admissions on 2026-10-19 at 02:12 UTC.
-- Change only the current homelab intake cap. Preserve concurrency, budgets,
-- retries, ownership controls and pinned receipts. Leave other operator caps.
WITH before AS (
    SELECT id, policy_json::jsonb AS policy
    FROM swarm.factory_control
    WHERE id = 'factory'
      AND policy_json::jsonb ->> 'repo' = 'jomcgi-org/homelab'
      AND policy_json::jsonb #> '{intake,max_per_day}' = '50'::jsonb
    FOR UPDATE
),
updated AS (
    UPDATE swarm.factory_control AS control
    SET policy_json = jsonb_set(before.policy, '{intake,max_per_day}', '500'::jsonb, false)::text,
        version = control.version + 1,
        actor = 'migration:20261019021500_factory_daily_admission_limit',
        updated_at = now()
    FROM before
    WHERE control.id = before.id
    RETURNING control.version
)
INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json)
SELECT
    'migration:20261019021500_factory_daily_admission_limit',
    'policy_migrated',
    NULL,
    jsonb_build_object(
        'migration', '20261019021500_factory_daily_admission_limit',
        'version', updated.version,
        'before', jsonb_build_object('intake', jsonb_build_object('max_per_day', 50)),
        'after', jsonb_build_object('intake', jsonb_build_object('max_per_day', 500))
    )::text
FROM updated;
