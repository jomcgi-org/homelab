-- Private operator report. Run with psql -X -f delivery_timings.sql against
-- the connector database. The cohort is work items created in the last 30 days.
WITH bounds AS (
    SELECT now() - interval '30 days' AS window_start, now() AS window_end
), items AS (
    SELECT w.*, p.created_at AS pr_at, p.merged_at
    FROM work_item w CROSS JOIN bounds b
    LEFT JOIN pull_request p ON p.work_item_id = w.id
    WHERE w.created_at >= b.window_start AND w.created_at < b.window_end
), observations AS (
    SELECT i.*, initial.created_at AS queued_at, a.first_sent_at,
        (SELECT min(r.created_at) FROM run r
         WHERE r.origin = 'reviewer' AND r.issue_key = i.issue_key
           AND r.created_at >= i.pr_at) AS review_at
    FROM items i
    LEFT JOIN LATERAL (
        SELECT id, created_at FROM run
        WHERE work_item_id = i.id AND origin = 'itsaplan'
        ORDER BY created_at, id LIMIT 1
    ) initial ON true
    LEFT JOIN delivery_attempt a ON a.run_id = initial.id AND a.seq = 1
), samples AS (
    SELECT id, metric, value FROM observations
    CROSS JOIN LATERAL (VALUES
        ('queue wait', extract(epoch FROM first_sent_at - queued_at)),
        ('time to first PR', extract(epoch FROM pr_at - created_at)),
        ('review wait', extract(epoch FROM review_at - pr_at)),
        ('rework rounds', review_rounds),
        ('time to merge', extract(epoch FROM merged_at - created_at))
    ) m(metric, value)
)
SELECT b.window_start, b.window_end, m.metric, m.unit,
    count(s.id) AS eligible, count(s.value) FILTER (WHERE s.value >= 0) AS measured,
    round(avg(s.value) FILTER (WHERE s.value >= 0), 2) AS mean
FROM bounds b CROSS JOIN (VALUES
    ('queue wait', 'seconds'), ('time to first PR', 'seconds'),
    ('review wait', 'seconds'), ('rework rounds', 'rounds'), ('time to merge', 'seconds')
) m(metric, unit)
LEFT JOIN samples s USING (metric)
GROUP BY b.window_start, b.window_end, m.metric, m.unit
ORDER BY m.metric;
