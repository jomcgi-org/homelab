-- Run in itsaplan, whose completed columns and status history define delivery.
-- Include manual Done moves, archived issues, and issues reopened after Done.
-- report-delivered-usage.sh loads the required pg_temp.connector_run_usage.
-- Shared positive run IDs appear once; negative connector rounds have no
-- agent_run row. Prefer local measurements (including canceled runs whose
-- result itsaplan can no longer accept), falling back to itsaplan's values.
WITH all_runs AS (
    SELECT coalesce(a.id, r.run_id) AS id,
           coalesce(a.issue_id, r.issue_id) AS issue_id,
           coalesce(r.input_tokens, a.input_tokens) AS input_tokens,
           coalesce(r.output_tokens, a.output_tokens) AS output_tokens
    FROM agent_run a
    FULL JOIN pg_temp.connector_run_usage r
      ON r.origin = 'itsaplan' AND r.run_id = a.id
)
SELECT p.key || '-' || i.sequence_number AS issue_key,
       count(r.id) AS runs,
       count(r.id) FILTER (
           WHERE r.input_tokens IS NULL OR r.output_tokens IS NULL
       ) AS unmeasured_runs,
       sum(r.input_tokens) AS input_tokens,
       sum(r.output_tokens) AS output_tokens,
       sum(r.input_tokens::bigint + r.output_tokens::bigint) AS total_tokens
FROM issue i
JOIN project p ON p.id = i.project_id
JOIN project_column c ON c.id = i.column_id
LEFT JOIN all_runs r ON r.issue_id = i.id
WHERE p.key = 'HOME'
  AND (
      c.state_type = 'completed'
      OR EXISTS (
          SELECT 1 FROM issue_activity a
          WHERE a.issue_id = i.id AND a.kind = 'activity' AND a.action = 'status'
            AND (a.payload #>> '{to,stateType}' = 'completed'
                 OR a.payload #>> '{from,stateType}' = 'completed')
      )
  )
GROUP BY p.id, p.key, i.id, i.sequence_number
ORDER BY p.key, i.sequence_number;
