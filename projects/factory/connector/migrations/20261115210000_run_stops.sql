-- A new admission may resume the work item before dispatch observes its stop.
-- Keep that stop on every already-admitted run, including transport redelivery.
-- NULL means no stop; an empty reason still requests a stop.
ALTER TABLE run ADD COLUMN stopped_reason text;

UPDATE run r SET stopped_reason = coalesce(w.stopped_reason, '')
FROM work_item w
WHERE r.work_item_id = w.id AND w.state = 'stopped'
    AND r.state IN ('claimed', 'waiting', 'dispatched', 'stale');
