-- Stars domain: curated dark-sky sites' future viewing windows.
-- One row per site-hour. Static site metadata (name/lat/lon/altitude/lp_zone)
-- lives in stars/seed.py and is joined in at read time. The hourly prune job and
-- the read endpoint both drop hours once their clock hour ends.

CREATE SCHEMA IF NOT EXISTS stars;

CREATE TABLE stars.site_hours (
    site_id     TEXT NOT NULL,
    hour_time   TIMESTAMPTZ NOT NULL,
    score       DOUBLE PRECISION NOT NULL,
    cloud_area_fraction DOUBLE PRECISION NOT NULL,
    relative_humidity   DOUBLE PRECISION NOT NULL,
    wind_speed          DOUBLE PRECISION NOT NULL,
    air_temperature     DOUBLE PRECISION NOT NULL,
    dew_spread          DOUBLE PRECISION NOT NULL,
    symbol      TEXT NOT NULL DEFAULT '',
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, hour_time)
);

CREATE INDEX idx_stars_site_hours_time  ON stars.site_hours (hour_time);
CREATE INDEX idx_stars_site_hours_score ON stars.site_hours (score DESC);
