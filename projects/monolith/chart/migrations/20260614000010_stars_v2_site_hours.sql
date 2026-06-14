-- Stars v2 (clear-dark-hours metric): drop the Q-derived columns from
-- stars.site_hours. The new metric counts clear dark hours and needs only
-- sun_elevation_deg and cloud_area_fraction (both already present). The
-- continuous quality score and its darkness/cloud factor decomposition are gone.
ALTER TABLE stars.site_hours DROP COLUMN IF EXISTS score;
ALTER TABLE stars.site_hours DROP COLUMN IF EXISTS darkness_factor;
ALTER TABLE stars.site_hours DROP COLUMN IF EXISTS cloud_factor;
