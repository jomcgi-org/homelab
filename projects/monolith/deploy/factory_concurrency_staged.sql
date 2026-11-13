-- INACTIVE #7378. Outside Atlas inputs. See ARCHITECTURE.md for activation.
WITH before AS (
    SELECT id, policy_json::jsonb AS policy
    FROM swarm.factory_control
    WHERE id = 'factory'
      AND policy_json::jsonb ->> 'repo' = 'jomcgi-org/homelab'
      AND policy_json::jsonb -> 'max_tasks' = jsonb_build_object('delivery', 10, 'advisory', 2)
      -- Sparse repository entries inherit the global cap. Refuse rather than
      -- changing another repository's effective limits or overriding a custom
      -- primary entry. A later operator decision must pin those maps first.
      AND policy_json::jsonb #> '{repos,jomcgi-org/homelab,max_tasks}' IS NULL
      AND NOT EXISTS (
          SELECT 1
          FROM jsonb_each(COALESCE(policy_json::jsonb -> 'repos', '{}'::jsonb)) AS peer(slug, settings)
          WHERE peer.slug <> 'jomcgi-org/homelab'
            AND (peer.settings -> 'max_tasks' IS NULL
                 OR peer.settings -> 'max_tasks' = 'null'::jsonb)
      )
    FOR UPDATE
),
updated AS (
    UPDATE swarm.factory_control AS control
    SET policy_json = jsonb_set(before.policy, '{max_tasks}', jsonb_build_object('delivery', 40, 'advisory', 4), false)::text,
        version = control.version + 1,
        actor = 'migration:20261113000000_factory_concurrency_staged',
        updated_at = now()
    FROM before
    WHERE control.id = before.id
    RETURNING control.version
)
INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json)
SELECT
    'migration:20261113000000_factory_concurrency_staged',
    'policy_migrated',
    NULL,
    jsonb_build_object(
        'migration', '20261113000000_factory_concurrency_staged',
        'version', updated.version,
        'before', jsonb_build_object('max_tasks', jsonb_build_object('delivery', 10, 'advisory', 2)),
        'after', jsonb_build_object('max_tasks', jsonb_build_object('delivery', 40, 'advisory', 4))
    )::text
FROM updated;
