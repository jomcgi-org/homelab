"""Local, run-by-hand backfill that populates the trips Postgres schema.

Re-derives points from image EXIF in the configured R2 source bucket,
optionally adds route-only "gap" points from KML directions and elevation from
the NRCan CDEM API, then replaces trips.points and optionally upserts trips.trips.
The //projects/monolith:trips_backfill CLI is excluded from the server image.
See main.py for authenticated R2 configuration, optional operator-supplied YAML
and the destructive-backfill warning. No S3 port-forward is needed.
"""
