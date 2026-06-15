-- trips schema: road-trip photo journeys, ported from the standalone
-- trips.jomcgi.dev service into the monolith (/app/trips, SSR).
--
-- trips  : one row per trip, holding the display metadata that used to live
--          in the frontend config.yaml (title/subtitle, per-day labels,
--          highlights, manual stats). days/highlights/stats are JSONB so the
--          shape can evolve without a migration per field.
-- points : one row per map point, keyed by (trip_slug, id). Mirrors the old
--          NATS TripPoint: GPS, capture time, the S3 image key, source, tags,
--          elevation (NRCan CDEM) and the camera EXIF/optics fields. Points
--          with image NULL are route-only "gap" points used to draw the
--          driving line between photos.
--
-- Points are re-derived from image EXIF in the `trips` S3 bucket by the local
-- backfill (projects/monolith/trips/backfill); the monolith never writes here
-- at runtime.

CREATE SCHEMA IF NOT EXISTS trips;

CREATE TABLE trips.trips (
    slug          TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    short_title   TEXT,
    subtitle      TEXT,
    -- IANA zone the camera-local EXIF timestamps are interpreted in when the
    -- backfill converts them to the tz-aware taken_at below.
    timezone      TEXT NOT NULL DEFAULT 'America/Vancouver',
    default_image TEXT,
    default_zoom  INTEGER,
    days          JSONB NOT NULL DEFAULT '{}'::jsonb,
    highlights    JSONB NOT NULL DEFAULT '[]'::jsonb,
    stats         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE trips.points (
    trip_slug         TEXT NOT NULL REFERENCES trips.trips (slug) ON DELETE CASCADE,
    id                TEXT NOT NULL,
    lat               DOUBLE PRECISION NOT NULL,
    lng               DOUBLE PRECISION NOT NULL,
    taken_at          TIMESTAMPTZ NOT NULL,
    image             TEXT,
    source            TEXT NOT NULL DEFAULT 'gopro',
    tags              TEXT[] NOT NULL DEFAULT '{}',
    elevation         DOUBLE PRECISION,
    light_value       DOUBLE PRECISION,
    iso               INTEGER,
    shutter_speed     TEXT,
    aperture          DOUBLE PRECISION,
    focal_length_35mm INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trip_slug, id)
);

-- Read path renders points in capture order per trip.
CREATE INDEX idx_trips_points_trip_taken ON trips.points (trip_slug, taken_at);
