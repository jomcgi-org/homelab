-- hikes schema: WalkHighlands walk corpus + met.no weather windows.
--
-- walks : one row per walk, keyed by the uuid5-of-coordinates identity from
--         the original scraper so re-scrapes upsert cleanly. The windows
--         column holds compact viable-window tuples
--         ([timestamp, temp_c, precip_mm, wind_kmh, cloud_pct]) replaced
--         wholesale by the 6-hourly forecast job.

CREATE SCHEMA IF NOT EXISTS hikes;

CREATE TABLE hikes.walks (
    uuid                TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    url                 TEXT NOT NULL,
    distance_km         DOUBLE PRECISION NOT NULL,
    ascent_m            INTEGER NOT NULL,
    duration_h          DOUBLE PRECISION NOT NULL,
    summary             TEXT NOT NULL DEFAULT '',
    latitude            DOUBLE PRECISION NOT NULL,
    longitude           DOUBLE PRECISION NOT NULL,
    windows             JSONB NOT NULL DEFAULT '[]'::jsonb,
    windows_updated_at  TIMESTAMPTZ,
    scraped_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
