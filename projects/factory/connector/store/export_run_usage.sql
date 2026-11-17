-- Export every issue-linked run, including connector-owned revision rounds.
-- itsaplan determines the Done cohort; local work-item state does not filter it.
SELECT r.id AS run_id, coalesce(w.issue_id, r.issue_id) AS issue_id, r.origin,
       r.input_tokens, r.output_tokens
FROM run r
LEFT JOIN work_item w ON w.id = r.work_item_id
WHERE coalesce(w.issue_id, r.issue_id) IS NOT NULL
ORDER BY r.id;
