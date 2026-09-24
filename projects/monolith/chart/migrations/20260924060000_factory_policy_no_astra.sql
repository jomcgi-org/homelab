-- One-shot GitOps policy change: move the live factory policy off GPT-6 Astra.
--
-- Conductor and reviewer run on opus, workers on spark, and every model pool
-- is replaced so no pool names astra. allowed_models keeps whatever the live
-- policy allowed, gains opus, sol and spark, and loses astra. Every other key,
-- generation included, is left as it is. Active tasks keep their own pinned
-- factory_receipt.policy_json, so only future admissions see the change.
--
-- The guard makes this a no-op on a re-run and on any policy an operator has
-- already moved off astra through the control surface.
--
-- allowed_models is deduplicated and ordered with COLLATE "C", which is the
-- codepoint order of validate_policy's sorted(set(...)).
--
-- policy_json is written back as jsonb text, so its bytes differ from
-- factory_controls._json (sort_keys, compact separators). Every reader,
-- including set_control's _policy_for_generation_comparison, compares
-- json.loads dicts rather than bytes, so the format does not matter.
--
-- The UPDATE and its audit row are one statement: the audit row exists only
-- when the guarded UPDATE changed the control row.
WITH before AS (
    SELECT id, policy_json::jsonb AS policy
    FROM swarm.factory_control
    WHERE id = 'factory'
      AND (
          policy_json::jsonb ->> 'conductor_model' = 'astra'
          OR policy_json::jsonb -> 'allowed_models' ? 'astra'
      )
    FOR UPDATE
),
after AS (
    SELECT
        id,
        policy AS before_policy,
        policy
            || jsonb_build_object(
                'conductor_model', 'opus',
                'worker_model', 'spark',
                'reviewer_model', 'opus',
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
                            SELECT unnest(ARRAY['opus', 'sol', 'spark'])
                        ) AS merged
                        WHERE model <> 'astra'
                    ) AS distinct_models
                ),
                'model_pools', jsonb_build_object(
                    'conductor', jsonb_build_array('opus', 'sol'),
                    'worker', jsonb_build_array('spark', 'sol'),
                    'implement', jsonb_build_array('spark', 'sol'),
                    'reviewer', jsonb_build_array('opus'),
                    'refine', jsonb_build_array('spark', 'sol')
                )
            ) AS after_policy
    FROM before
),
updated AS (
    UPDATE swarm.factory_control AS control
    SET policy_json = after.after_policy::text,
        version = control.version + 1,
        actor = 'migration:20260924060000_factory_policy_no_astra',
        updated_at = now()
    FROM after
    WHERE control.id = after.id
    RETURNING control.version
)
INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json)
SELECT
    'migration:20260924060000_factory_policy_no_astra',
    'policy_migrated',
    NULL,
    jsonb_build_object(
        'migration', '20260924060000_factory_policy_no_astra',
        'version', updated.version,
        'before', jsonb_build_object(
            'conductor_model', after.before_policy -> 'conductor_model',
            'worker_model', after.before_policy -> 'worker_model',
            'reviewer_model', after.before_policy -> 'reviewer_model',
            'allowed_models', after.before_policy -> 'allowed_models',
            'model_pools', after.before_policy -> 'model_pools'
        ),
        'after', jsonb_build_object(
            'conductor_model', after.after_policy -> 'conductor_model',
            'worker_model', after.after_policy -> 'worker_model',
            'reviewer_model', after.after_policy -> 'reviewer_model',
            'allowed_models', after.after_policy -> 'allowed_models',
            'model_pools', after.after_policy -> 'model_pools'
        )
    )::text
FROM after
CROSS JOIN updated;
