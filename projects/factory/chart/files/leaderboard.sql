-- Pool the view's cohorts by arm. Medians require the original observations.
WITH counts AS (
    SELECT arm, sum(n) AS n, sum(round(merge_rate * n)) AS merges,
           sum(round(revert_rate * round(merge_rate * n))) AS reverts
    FROM arm_delivery_results WHERE arm IS NOT NULL GROUP BY arm
), medians AS (
    SELECT arm, percentile_cont(0.5) WITHIN GROUP (ORDER BY review_rounds) AS median_rounds,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY first_build_minutes + rework_minutes)
               FILTER (WHERE merged) AS median_minutes
    FROM pr_delivery_results WHERE arm IS NOT NULL GROUP BY arm
)
SELECT coalesce(json_agg(row_to_json(result) ORDER BY arm), '[]') FROM (
    SELECT arm, n, merges, coalesce(reverts, 0) AS reverts, median_rounds, median_minutes
    FROM counts JOIN medians USING (arm)
) result;
