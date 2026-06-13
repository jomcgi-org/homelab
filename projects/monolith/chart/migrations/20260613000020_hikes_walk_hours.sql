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

-- Backfill the existing JSONB windows into typed rows BEFORE dropping the
-- column, so a deploy does not blank /app/hikes until the next 2-hourly
-- refresh (the refresh job keeps its stored next_run_at across the rollout, so
-- the gap would otherwise be up to ~2 h). Each windows element is a
-- [ts_unix, temp_c, precip_mm, wind_kmh, cloud_pct] tuple; windows_updated_at
-- carries over as fetched_at so freshness (the list ETag/generated_at) survives
-- the migration. ON CONFLICT guards against any duplicate hour in a stored
-- array. This transforms rows already in Postgres, so the migration file stays
-- tiny (well under the 256 KiB migrations-ConfigMap annotation cap).
INSERT INTO hikes.walk_hours
    (walk_uuid, hour_time, temp_c, precip_mm, wind_kmh, cloud_pct, fetched_at)
SELECT
    w.uuid,
    to_timestamp((e->>0)::double precision),
    (e->>1)::double precision,
    (e->>2)::double precision,
    (e->>3)::double precision,
    (e->>4)::double precision,
    COALESCE(w.windows_updated_at, now())
FROM hikes.walks w
CROSS JOIN LATERAL jsonb_array_elements(w.windows) AS e
ON CONFLICT (walk_uuid, hour_time) DO NOTHING;

ALTER TABLE hikes.walks DROP COLUMN windows;
ALTER TABLE hikes.walks DROP COLUMN windows_updated_at;
