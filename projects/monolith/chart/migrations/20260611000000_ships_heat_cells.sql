-- ships.heat_cells: precomputed traffic-density rollup for the /app/ships heatmap.
--
-- One row per occupied ~500m grid cell, holding the count of position fixes that
-- fell in that cell over the trailing retention window. Recomputed in full from
-- ships.positions by an hourly scheduled rollup (ships.heat_rollup). positions is
-- the 7-day partitioned source of truth; this table just caches the aggregate so
-- the map can be served cheaply (the live GROUP BY over millions of rows is too
-- heavy to run per request).
--
-- The cell index is floor(lat / 0.005) x floor(lon / 0.0075), a ~500m square at
-- the Salish Sea latitude (~48N, where a degree of longitude is shorter than a
-- degree of latitude). The serving layer reconstructs each cell's polygon as
-- [lat_bin*step_lat, lon_bin*step_lon] .. [+step_lat, +step_lon].

CREATE TABLE ships.heat_cells (
    lat_bin      INTEGER NOT NULL,
    lon_bin      INTEGER NOT NULL,
    count        INTEGER NOT NULL,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lat_bin, lon_bin)
);
