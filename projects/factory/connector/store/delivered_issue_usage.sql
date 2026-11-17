-- All runs of each delivered issue, including failed and connector-owned runs.
-- Cached input is already part of input_tokens. Unknown runs remain unknown.
SELECT w.issue_key,
       count(*) AS runs,
       count(*) FILTER (WHERE r.input_tokens IS NULL) AS unmeasured_runs,
       sum(r.input_tokens) AS input_tokens,
       sum(r.cached_input_tokens) AS cached_input_tokens,
       sum(r.output_tokens) AS output_tokens,
       sum(r.input_tokens + r.output_tokens) AS total_tokens
FROM work_item w
JOIN run r ON r.work_item_id = w.id
WHERE w.state = 'done'
  AND EXISTS (
      SELECT 1 FROM outbox o
      WHERE o.kind = 'issue_done'
        AND o.payload ->> 'issueId' = w.issue_id::text
        AND o.delivered_at IS NOT NULL
        AND o.abandoned_at IS NULL
  )
GROUP BY w.issue_key
ORDER BY w.issue_key;
