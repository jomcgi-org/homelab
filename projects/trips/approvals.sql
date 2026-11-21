-- Apply after restoring the hub trips schema. Each approved trip includes its
-- display metadata, default image and highlight images. Approve points separately.
CREATE TABLE IF NOT EXISTS trips.public_trips (
    slug text PRIMARY KEY REFERENCES trips.trips ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trips.public_points (
    trip_slug text NOT NULL REFERENCES trips.public_trips ON DELETE CASCADE,
    id text NOT NULL,
    PRIMARY KEY (trip_slug, id),
    FOREIGN KEY (trip_slug, id) REFERENCES trips.points ON DELETE CASCADE
);
