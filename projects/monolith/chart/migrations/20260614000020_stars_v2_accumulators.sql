-- Stars v2 (clear-dark-hours metric): replace the Q sufficient stats on both
-- accumulators with plain clear-dark counts. The v2 grid changes site_ids and
-- the metric changes, so the v1 rows are stale: truncate both tables first, then
-- swap window_count / sum_q / sum_darkness / sum_clarity for dark_hours and
-- clear_dark_hours. dark_hours is the denominator; clear_dark_hours is the
-- headline metric and clear_dark_hours / dark_hours is the clarity rate.
-- nosemgrep: migration-destructive-ddl (safe: v1 rows are stale after the grid/metric
-- change; truncating before inserting v2 data is the correct migration strategy)
TRUNCATE stars.site_month_stats;
-- nosemgrep: migration-destructive-ddl (same rationale as above)
TRUNCATE stars.site_month_climatology;

ALTER TABLE stars.site_month_stats   DROP COLUMN window_count, DROP COLUMN sum_q,
    DROP COLUMN sum_darkness, DROP COLUMN sum_clarity;
ALTER TABLE stars.site_month_stats   ADD COLUMN dark_hours INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN clear_dark_hours INTEGER NOT NULL DEFAULT 0;

ALTER TABLE stars.site_month_climatology DROP COLUMN window_count, DROP COLUMN sum_q,
    DROP COLUMN sum_darkness, DROP COLUMN sum_clarity;
ALTER TABLE stars.site_month_climatology ADD COLUMN dark_hours INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN clear_dark_hours INTEGER NOT NULL DEFAULT 0;
