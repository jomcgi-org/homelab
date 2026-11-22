/* invariant: HOME-756-3 */
-- HOME-756: a review-capped PR whose review still has a critical or high
-- finding is re-cut in a fresh session. recuts bounds the re-cuts of a work
-- item. recut_pr and recut_head journal the close of the capped PR before
-- GitHub is called, in the same transaction as the state change; the next
-- send or poll closes recut_pr and sets recut_closed. The replaced
-- pull_request row has the state 'recut' until the new PR's row replaces it,
-- and counts as an abandoned delivery outcome.
ALTER TABLE work_item
    ADD COLUMN recuts integer NOT NULL DEFAULT 0 CHECK (recuts >= 0),
    ADD COLUMN recut_pr integer NOT NULL DEFAULT 0,
    ADD COLUMN recut_head text NOT NULL DEFAULT '',
    ADD COLUMN recut_closed boolean NOT NULL DEFAULT true;

CREATE OR REPLACE FUNCTION sync_delivery_outcome(wid bigint, observed_pr boolean DEFAULT false) RETURNS void LANGUAGE sql AS $$
    INSERT INTO delivery_outcome(node_id, work_item_id, number, merged_at, merge_sha, abandoned)
    SELECT p.node_id, w.id, p.number, p.merged_at, p.merge_commit_sha, p.merged_at IS NULL
    FROM work_item w JOIN pull_request p ON p.work_item_id = w.id
    WHERE w.id = wid AND (p.merged_at IS NOT NULL OR p.state IN ('closed', 'recut') OR w.state = 'stopped')
    ON CONFLICT(node_id) DO UPDATE SET merged_at = excluded.merged_at,
        merge_sha = excluded.merge_sha, abandoned = excluded.abandoned;
    DELETE FROM delivery_outcome o USING work_item w, pull_request p
    WHERE observed_pr AND o.abandoned AND w.id = wid AND p.work_item_id = wid AND o.node_id = p.node_id
        AND w.state <> 'stopped' AND p.state = 'open';
    UPDATE run r SET outcome_node = o.node_id
    FROM delivery_outcome o LEFT JOIN pull_request p ON p.node_id=o.node_id
    WHERE o.work_item_id=wid AND NOT o.metrics_recorded AND p.id IS NOT NULL
      AND r.work_item_id=wid AND r.origin <> 'reviewer' AND r.outcome_node IS NULL;
    SELECT refresh_delivery_metrics(o.node_id) FROM delivery_outcome o
    LEFT JOIN pull_request p ON p.node_id=o.node_id
    WHERE o.work_item_id=wid AND NOT o.metrics_recorded AND p.id IS NOT NULL;
$$;
