-- ships.heat_cells_historical: monotonic all-time traffic-density accumulator.
--
-- One row per ~500m grid cell holding the cumulative "vessel-days" of traffic:
-- the running SUM of each day's count(distinct moving mmsi) for that cell. Rows
-- are banked by ships.retention._run_partition_maintenance just before a daily
-- partition of ships.positions is dropped, so data that ages out of the live
-- 7-day window (ships.heat_cells) is preserved here instead of being lost.
--
-- Cell index matches ships.heat_cells: floor(lat / 0.005) x floor(lon / 0.0075).
-- The serving layer sums this table with the live ships.heat_cells to render the
-- all-time map. Banking is additive (ON CONFLICT DO UPDATE count = count +
-- EXCLUDED.count) and idempotent because the source partition no longer exists
-- on retry.

CREATE TABLE ships.heat_cells_historical (
    lat_bin     INTEGER NOT NULL,
    lon_bin     INTEGER NOT NULL,
    count       BIGINT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lat_bin, lon_bin)
);
