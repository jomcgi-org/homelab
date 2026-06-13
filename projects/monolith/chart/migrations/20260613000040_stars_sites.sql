-- Stars domain: site list sourced from a light-pollution grid (ADR 006).
-- Replaces the curated stars/seed.py list as the source of sites. The
-- stars.load_grid job ingests grid.json from SeaweedFS into this table, and the
-- refresh job iterates these rows instead of the seed list.

CREATE TABLE stars.sites (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    lat         DOUBLE PRECISION NOT NULL,
    lon         DOUBLE PRECISION NOT NULL,
    altitude_m  INTEGER NOT NULL DEFAULT 0,
    lp_zone     TEXT NOT NULL DEFAULT 'unknown',
    source      TEXT NOT NULL DEFAULT 'grid',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
