-- A new admission may resume a work item before dispatch observes its stop.
-- Keep that stop on every already-admitted run, including transport redelivery.
-- NULL means no stop; an empty reason still requests a stop.
ALTER TABLE run ADD COLUMN stopped_reason text;

UPDATE run r SET stopped_reason = coalesce(w.stopped_reason, '')
FROM work_item w
WHERE r.work_item_id = w.id AND w.state = 'stopped'
    AND r.state IN ('claimed', 'waiting', 'dispatched', 'stale');

-- Legacy brief-held runs may already have lost their admission owner without
-- an attempt for reassignment to cancel. Preserve any earlier explicit stop.
UPDATE run r SET stopped_reason = 'superseded: the work item was reassigned to another agent'
FROM work_item w
WHERE r.work_item_id = w.id AND r.agent_key <> w.agent_key
    AND r.stopped_reason IS NULL
    AND r.state IN ('claimed', 'waiting', 'dispatched', 'stale')
    AND NOT EXISTS (SELECT 1 FROM delivery_attempt a WHERE a.run_id = r.id);
