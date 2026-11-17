-- A stop fences every older schedule, even after a new delegation is admitted.
-- Keep this row independent of work_item: a held first run has no work item.
-- Freeze legacy writes until both the backfill and its triggers are installed.
LOCK TABLE work_item, run, outbox IN SHARE ROW EXCLUSIVE MODE;

CREATE TABLE deferred_fence (
    issue_id bigint PRIMARY KEY,
    generation bigint NOT NULL DEFAULT 0,
    stopped boolean NOT NULL DEFAULT false
);

-- NULL means that legacy timestamps cannot establish the request's stop
-- boundary. Delivery holds it and posts an operator reconciliation notice.
ALTER TABLE outbox ADD COLUMN deferred_stop_generation bigint DEFAULT 0;

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

-- updated_at is an upper bound on the last historical stop: subsequent
-- admission, completion and session updates may move it forward. Requests
-- after that bound are safe to retain; all others need reconciliation.
WITH stop_bounds AS (
    SELECT issue_id, max(updated_at) AS latest
    FROM (
        SELECT issue_id, updated_at FROM work_item WHERE stops > 0
        UNION ALL
        SELECT issue_id, updated_at FROM run
        WHERE work_item_id IS NULL AND stopped_reason IS NOT NULL AND issue_id IS NOT NULL
    ) stops
    GROUP BY issue_id
)
UPDATE outbox o SET deferred_stop_generation =
    CASE WHEN NOT f.stopped AND o.created_at > b.latest THEN f.generation ELSE NULL END
FROM deferred_fence f JOIN stop_bounds b USING (issue_id)
WHERE o.kind = 'deferred_start' AND o.dedupe_key LIKE 'deferred/' || f.issue_id || '/%';

-- Keep rollback binaries on the same fence without changes to their code.
-- Deferred triggers run after the transaction has acquired its work-item and
-- run locks. Enqueue and send admission take only the fence and outbox locks.
CREATE FUNCTION deferred_work_fence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.stops > OLD.stops THEN
        INSERT INTO deferred_fence (issue_id, generation, stopped) VALUES (NEW.issue_id, 1, true)
        ON CONFLICT (issue_id) DO UPDATE SET generation = deferred_fence.generation + 1, stopped = true;
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER deferred_work_fence
AFTER UPDATE ON work_item DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION deferred_work_fence();

CREATE FUNCTION deferred_run_fence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    admitted_issue bigint;
BEGIN
    IF NEW.work_item_id IS NULL AND NEW.issue_id IS NOT NULL
        AND OLD.stopped_reason IS NULL AND NEW.stopped_reason IS NOT NULL THEN
        INSERT INTO deferred_fence (issue_id, generation, stopped) VALUES (NEW.issue_id, 1, true)
        ON CONFLICT (issue_id) DO UPDATE SET generation = deferred_fence.generation + 1, stopped = true;
    ELSIF OLD.work_item_id IS NULL AND NEW.work_item_id IS NOT NULL
        AND NEW.origin = 'itsaplan' AND NEW.stopped_reason IS NULL THEN
        SELECT issue_id INTO admitted_issue FROM work_item WHERE id = NEW.work_item_id;
        -- Explicit re-admission permits future schedules, preserving the
        -- generation that fences every request from before the stop.
        UPDATE deferred_fence SET stopped = false WHERE issue_id = admitted_issue;
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER deferred_run_fence
AFTER UPDATE ON run DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION deferred_run_fence();

-- A rollback binary can also enqueue a new request after re-admission.
-- Snapshot its boundary here instead of letting the column default attach
-- generation zero to a valid post-stop schedule.
CREATE FUNCTION deferred_request_fence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    request_issue bigint;
    current_generation bigint;
    currently_stopped boolean;
BEGIN
    IF NEW.kind = 'deferred_start' AND NEW.dedupe_key ~ '^deferred/[0-9]+/[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN
        request_issue := split_part(NEW.dedupe_key, '/', 2)::bigint;
        INSERT INTO deferred_fence (issue_id) VALUES (request_issue) ON CONFLICT DO NOTHING;
        SELECT generation, stopped INTO current_generation, currently_stopped
            FROM deferred_fence WHERE issue_id = request_issue FOR UPDATE;
        -- Even a request inserted while stopped cannot borrow a subsequent
        -- admission. The first stop always advances generation above zero.
        NEW.deferred_stop_generation := current_generation - CASE WHEN currently_stopped THEN 1 ELSE 0 END;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER deferred_request_fence BEFORE INSERT ON outbox
FOR EACH ROW EXECUTE FUNCTION deferred_request_fence();
