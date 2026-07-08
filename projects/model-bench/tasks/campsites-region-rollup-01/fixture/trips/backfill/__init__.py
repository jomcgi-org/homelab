"""Local, run-by-hand backfill that populates the trips Postgres schema.

Re-derives points from image EXIF in the `trips` S3 (SeaweedFS) bucket,
optionally adds route-only "gap" points from KML directions and elevation from
the NRCan CDEM API, then upserts into trips.trips / trips.points. Run via
`bazel run //projects/monolith:trips_backfill -- ...` against a port-forwarded
SeaweedFS + Postgres; it is not part of the monolith server image.
"""
