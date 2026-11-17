-- A stop fences every older schedule, even after a new delegation is admitted.
-- Keep this row independent of work_item: a held first run has no work item.
CREATE TABLE deferred_fence (
    issue_id bigint PRIMARY KEY,
    generation bigint NOT NULL DEFAULT 0,
    stopped boolean NOT NULL DEFAULT false
);

-- Existing requests have no stop witness. Zero conservatively fences them
-- when the issue has any recorded stop, including one followed by re-admission.
ALTER TABLE outbox ADD COLUMN deferred_stop_generation bigint NOT NULL DEFAULT 0;

INSERT INTO deferred_fence (issue_id, generation, stopped)
SELECT issue_id, 1,
    EXISTS (SELECT 1 FROM work_item w WHERE w.issue_id = issues.issue_id AND w.state = 'stopped')
    OR EXISTS (
        SELECT 1 FROM run r
        WHERE r.issue_id = issues.issue_id AND r.work_item_id IS NULL AND r.stopped_reason IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM run admitted
                WHERE admitted.issue_id = r.issue_id AND admitted.origin = 'itsaplan'
                    AND admitted.work_item_id IS NOT NULL AND admitted.id > r.id
                    -- Require evidence that the new claim followed the stop.
                    -- Later updates of the old run conservatively retain it.
                    AND admitted.created_at > r.updated_at
            )
    )
FROM (
    SELECT issue_id FROM work_item WHERE stops > 0
    UNION
    SELECT issue_id FROM run WHERE work_item_id IS NULL AND stopped_reason IS NOT NULL AND issue_id IS NOT NULL
) issues;
