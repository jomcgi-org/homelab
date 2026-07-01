-- BC Parks campsite availability x Open-Meteo weather for /app/campsites.
-- Wholly public data (no credentials needed to read from GoingToCamp or Open-Meteo),
-- granted directly to public_reader via the companion grant migration
-- (20260630150100_campsites_public_reader_grant.sql).

CREATE SCHEMA IF NOT EXISTS campsites;

-- Static catalog, refreshed weekly from GoingToCamp /api/resourceLocation + /api/maps.
CREATE TABLE campsites.campgrounds (
    resource_location_id BIGINT PRIMARY KEY,
    park_map_id          BIGINT NOT NULL,
    name                 TEXT   NOT NULL,
    region               TEXT   NOT NULL DEFAULT '',
    latitude             DOUBLE PRECISION NOT NULL,
    longitude            DOUBLE PRECISION NOT NULL,
    iana_tz              TEXT   NOT NULL DEFAULT 'America/Vancouver',
    description          TEXT   NOT NULL DEFAULT '',
    booking_url          TEXT   NOT NULL DEFAULT 'https://camping.bcparks.ca/',
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-park-per-date availability (loop-level OR), upserted hourly.
CREATE TABLE campsites.availability (
    resource_location_id BIGINT NOT NULL REFERENCES campsites.campgrounds(resource_location_id) ON DELETE CASCADE,
    date                 DATE   NOT NULL,
    has_availability     BOOLEAN NOT NULL DEFAULT FALSE,
    loops_open           INTEGER NOT NULL DEFAULT 0,
    scraped_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (resource_location_id, date)
);
CREATE INDEX idx_campsites_avail_date ON campsites.availability (date);

-- Per-park-per-date forecast plus computed clear-sky score, upserted hourly.
CREATE TABLE campsites.weather (
    resource_location_id BIGINT NOT NULL REFERENCES campsites.campgrounds(resource_location_id) ON DELETE CASCADE,
    date                 DATE   NOT NULL,
    cloud_cover          DOUBLE PRECISION,
    precip_sum           DOUBLE PRECISION,
    precip_prob          INTEGER,
    temp_max             DOUBLE PRECISION,
    wind_max             DOUBLE PRECISION,
    sunny_score          INTEGER NOT NULL DEFAULT 0,
    is_good              BOOLEAN NOT NULL DEFAULT FALSE,
    fetched_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (resource_location_id, date)
);
