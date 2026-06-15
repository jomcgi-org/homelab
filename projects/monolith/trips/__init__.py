"""trips: road-trip photo journeys, served SSR at /app/trips.

This package currently holds the Postgres-backed data model (models.py) and a
local, run-by-hand backfill (backfill/) that re-derives points from image EXIF
in the `trips` S3 bucket. The SSR router and scheduled jobs land in later
phases of the migration off the standalone trips.jomcgi.dev service.
"""
