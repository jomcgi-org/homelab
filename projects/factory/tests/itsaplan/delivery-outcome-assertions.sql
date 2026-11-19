DO $$
BEGIN
    IF (SELECT count(*) FROM delivery_outcome) <> 2 OR
       NOT EXISTS (SELECT FROM pr_delivery_results WHERE number=11 AND merged AND NOT abandoned
                   AND arm='control' AND guidance_version='base' AND tokens=150) OR
       NOT EXISTS (SELECT FROM pr_delivery_results WHERE work_item_id=2 AND number=12 AND abandoned) OR
       EXISTS (SELECT FROM delivery_outcome WHERE work_item_id=3) THEN
        RAISE EXCEPTION 'migration failed to backfill historical deliveries and stopped PRs';
    END IF;

    UPDATE delivery_outcome SET reverted_at=merged_at+interval '3 days' WHERE number=11;
    IF (SELECT count(*) FROM arm_delivery_results) <> 2 OR
       NOT EXISTS (SELECT FROM arm_delivery_results WHERE arm='control' AND task_class='feature'
                   AND n=1 AND merge_rate=1 AND tokens_per_merge=150 AND revert_rate=1) OR
       NOT EXISTS (SELECT FROM arm_delivery_results WHERE arm='candidate' AND task_class='fix'
                   AND n=1 AND merge_rate=0 AND tokens_per_merge IS NULL) THEN
        RAISE EXCEPTION 'unexpected per-arm results after backfill and revert';
    END IF;
END $$;
