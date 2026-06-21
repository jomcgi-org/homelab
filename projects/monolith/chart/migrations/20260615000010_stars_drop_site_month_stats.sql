-- Stars v2 cleanup: retire the live bank-at-prune accumulator. The historical
-- layer now comes entirely from stars.site_month_climatology (ERA5/CERRA
-- backfill, ADR 009), so the per-site live accumulator is redundant and the
-- prune only deletes elapsed forecast hours. Drop the now-unused table.
-- nosemgrep: migration-destructive-ddl (safe: table is retired; ERA5/CERRA backfill in
-- stars.site_month_climatology is the sole source of historical data; see ADR 009)
DROP TABLE IF EXISTS stars.site_month_stats;
