-- Stars domain: record the sun's elevation per site-hour.
-- ADR 007 ranks dark hours by a continuous quality Q = D x C x W where the
-- darkness factor D is derived from the sun elevation; storing the elevation
-- lets the read side reason about how dark each hour actually was.

ALTER TABLE stars.site_hours ADD COLUMN sun_elevation_deg DOUBLE PRECISION NOT NULL DEFAULT 0;
