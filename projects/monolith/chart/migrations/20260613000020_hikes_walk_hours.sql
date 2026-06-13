-- hikes typed forecast hours: replace the JSONB windows tuple-array on
-- hikes.walks with a typed, hour-keyed table mirroring stars.site_hours.
--
-- walk_hours : one row per walk-hour, keyed by (walk_uuid, hour_time). The
--              6-hourly forecast job wholesale-replaces a walk's rows; the
--              hourly prune job and both read endpoints drop hours once their
--              clock hour ends (shared.forecast_freshness.top_of_hour). This
--              stops elapsed hours leaking between the 2-hourly refreshes the
--              old JSONB array allowed.
--
-- The old windows / windows_updated_at columns on hikes.walks are dropped:
-- freshness now derives from max(walk_hours.fetched_at), so the marker column
-- is redundant.

CREATE TABLE hikes.walk_hours (
    walk_uuid   TEXT NOT NULL,
    hour_time   TIMESTAMPTZ NOT NULL,
    temp_c      DOUBLE PRECISION NOT NULL,
    precip_mm   DOUBLE PRECISION NOT NULL,
    wind_kmh    DOUBLE PRECISION NOT NULL,
    cloud_pct   DOUBLE PRECISION NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (walk_uuid, hour_time)
);

CREATE INDEX idx_hikes_walk_hours_time ON hikes.walk_hours (hour_time);

ALTER TABLE hikes.walks DROP COLUMN windows;
ALTER TABLE hikes.walks DROP COLUMN windows_updated_at;
