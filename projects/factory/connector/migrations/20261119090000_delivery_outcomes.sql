-- A stopped delivery may resume. A merged PR remains delivered even if its
-- work item subsequently stops. Keep PR identity across a replacement PR.
CREATE TABLE delivery_outcome (
    node_id text PRIMARY KEY,
    work_item_id bigint NOT NULL REFERENCES work_item(id),
    number integer,
    arm text,
    task_class text,
    guidance_version text,
    first_build_minutes numeric,
    rework_minutes numeric,
    review_rounds bigint,
    ci_fix_rounds bigint,
    tokens numeric,
    time_held interval,
    merged_at timestamptz,
    merge_sha text,
    abandoned boolean NOT NULL,
    reverted_at timestamptz,
    checked_until timestamptz,
    metrics_recorded boolean NOT NULL DEFAULT false,
    retry_at timestamptz,
    scan_until timestamptz,
    CHECK (abandoned = (merged_at IS NULL)),
    CHECK (reverted_at IS NULL OR (merged_at IS NOT NULL AND reverted_at BETWEEN merged_at AND merged_at + interval '168 hours'))
);

ALTER TABLE run ADD COLUMN outcome_node text REFERENCES delivery_outcome(node_id) ON DELETE SET NULL,
    ADD COLUMN queue_ci_fix boolean NOT NULL DEFAULT false;
CREATE INDEX run_outcome_idx ON run(outcome_node);

ALTER TABLE pull_request ADD COLUMN held_since timestamptz,
    ADD COLUMN held_duration interval NOT NULL DEFAULT interval '0';
ALTER TABLE run ADD COLUMN admission_held interval NOT NULL DEFAULT interval '0';

CREATE FUNCTION refresh_delivery_metrics(target text) RETURNS void LANGUAGE sql AS $$
UPDATE delivery_outcome dest
SET metrics_recorded = true,
    (arm, task_class, guidance_version, first_build_minutes, rework_minutes,
     review_rounds, ci_fix_rounds, tokens, time_held) =
    (m.arm, m.task_class, m.guidance_version, m.first_build_minutes, m.rework_minutes,
     m.review_rounds, m.ci_fix_rounds, m.tokens, m.time_held)
FROM (
    SELECT o.node_id, f.arm, f.task_class, f.base_sha AS guidance_version,
           minutes.first_build_minutes, minutes.rework_minutes,
           rounds.review_rounds, rounds.ci_fix_rounds + usage.queue_fixes AS ci_fix_rounds, usage.tokens,
           coalesce(usage.time_held, interval '0') + coalesce(pr.held_duration, interval '0') AS time_held
    FROM delivery_outcome o
    LEFT JOIN pull_request pr ON pr.node_id = o.node_id
    LEFT JOIN LATERAL (
        SELECT id, arm, task_class, base_sha FROM run
        WHERE work_item_id = o.work_item_id AND origin <> 'reviewer'
        ORDER BY created_at, id LIMIT 1
    ) f ON true
    CROSS JOIN LATERAL (
        SELECT coalesce(sum(extract(epoch FROM a.finished_at - a.first_sent_at)/60)
            FILTER (WHERE r.id = f.id), 0) AS first_build_minutes,
            coalesce(sum(extract(epoch FROM a.finished_at - a.first_sent_at)/60)
            FILTER (WHERE r.id <> f.id), 0) AS rework_minutes
        FROM run r JOIN delivery_attempt a ON a.run_id = r.id
        WHERE r.outcome_node = o.node_id
    ) minutes
    CROSS JOIN LATERAL (
        SELECT CASE WHEN count(input_tokens + output_tokens) = count(*)
            THEN coalesce(sum(input_tokens + output_tokens), 0) END AS tokens,
            count(*) FILTER (WHERE queue_ci_fix) AS queue_fixes, sum(admission_held) AS time_held
        FROM run WHERE outcome_node = o.node_id
    ) usage
    CROSS JOIN LATERAL (
        SELECT count(DISTINCT round_run_id) FILTER (WHERE kind = 'changes_requested') AS review_rounds,
            count(DISTINCT round_run_id) FILTER (WHERE kind IN ('check_failed','status_failed')) AS ci_fix_rounds
        FROM review_event e JOIN run r ON r.id=e.round_run_id
        WHERE r.outcome_node = o.node_id AND disposition = 'acted'
    ) rounds WHERE o.node_id=target
) m WHERE dest.node_id=m.node_id;
$$;

CREATE FUNCTION sync_delivery_outcome(wid bigint, observed_pr boolean DEFAULT false) RETURNS void LANGUAGE sql AS $$
    DELETE FROM delivery_outcome WHERE work_item_id = wid AND number IS NULL;
    INSERT INTO delivery_outcome(node_id, work_item_id, abandoned)
    SELECT 'work-item/' || w.id, w.id, true FROM work_item w
    WHERE w.id = wid AND w.state = 'stopped'
      AND NOT EXISTS (SELECT 1 FROM pull_request WHERE work_item_id = wid);
    INSERT INTO delivery_outcome(node_id, work_item_id, number, merged_at, merge_sha, abandoned)
    SELECT p.node_id, w.id, p.number, p.merged_at, p.merge_commit_sha, p.merged_at IS NULL
    FROM work_item w JOIN pull_request p ON p.work_item_id = w.id
    WHERE w.id = wid AND (p.merged_at IS NOT NULL OR p.state = 'closed' OR w.state = 'stopped')
    ON CONFLICT(node_id) DO UPDATE SET merged_at = excluded.merged_at,
        merge_sha = excluded.merge_sha, abandoned = excluded.abandoned;
    DELETE FROM delivery_outcome o USING work_item w, pull_request p
    WHERE observed_pr AND o.abandoned AND w.id = wid AND p.work_item_id = wid AND o.node_id = p.node_id
        AND w.state <> 'stopped' AND p.state = 'open';
    UPDATE run r SET outcome_node = o.node_id
    FROM delivery_outcome o LEFT JOIN pull_request p ON p.node_id=o.node_id
    WHERE o.work_item_id=wid AND NOT o.metrics_recorded AND (o.number IS NULL OR p.id IS NOT NULL)
      AND r.work_item_id=wid AND r.origin <> 'reviewer' AND r.outcome_node IS NULL;
    SELECT refresh_delivery_metrics(o.node_id) FROM delivery_outcome o
    LEFT JOIN pull_request p ON p.node_id=o.node_id
    WHERE o.work_item_id=wid AND NOT o.metrics_recorded AND (o.number IS NULL OR p.id IS NOT NULL);
$$;
CREATE FUNCTION record_pr_outcome() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sync_delivery_outcome(NEW.work_item_id, true);
    RETURN NULL;
END $$;
CREATE TRIGGER pr_outcome AFTER INSERT OR UPDATE OF state, merged_at, node_id ON pull_request
    FOR EACH ROW EXECUTE FUNCTION record_pr_outcome();
CREATE FUNCTION record_work_outcome() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sync_delivery_outcome(NEW.id);
    RETURN NULL;
END $$;
CREATE TRIGGER work_outcome AFTER UPDATE OF state ON work_item
    FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state) EXECUTE FUNCTION record_work_outcome();
SELECT sync_delivery_outcome(id) FROM work_item;

-- Late usage receipts belong to the run's assigned PR, even after replacement.
CREATE FUNCTION refresh_run_usage() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE delivery_outcome SET tokens = (
        SELECT CASE WHEN count(input_tokens + output_tokens)=count(*) THEN sum(input_tokens + output_tokens) END
        FROM run WHERE outcome_node=NEW.outcome_node
    ) WHERE node_id=NEW.outcome_node;
    RETURN NULL;
END $$;
CREATE TRIGGER outcome_usage AFTER UPDATE OF input_tokens,output_tokens ON run
    FOR EACH ROW WHEN (NEW.outcome_node IS NOT NULL) EXECUTE FUNCTION refresh_run_usage();

CREATE VIEW pr_delivery_results AS
SELECT o.*, merged_at IS NOT NULL AS merged, reverted_at IS NOT NULL AS reverted FROM delivery_outcome o;

CREATE VIEW arm_delivery_results AS
SELECT arm, task_class, guidance_version, count(*) AS n, avg(merged::int) AS merge_rate,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY review_rounds) AS median_review_rounds,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY first_build_minutes + rework_minutes)
        FILTER (WHERE merged) AS median_coder_minutes_per_merge,
    CASE WHEN count(tokens) = count(*) THEN sum(tokens) / nullif(count(*) FILTER (WHERE merged),0) END AS tokens_per_merge,
    avg(reverted::int) FILTER (WHERE merged) AS revert_rate
FROM pr_delivery_results GROUP BY arm, task_class, guidance_version;
