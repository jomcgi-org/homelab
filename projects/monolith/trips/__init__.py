"""trips: road-trip photo journeys, served SSR at /app/trips.

This package currently holds the Postgres-backed data model (models.py) and a
local, run-by-hand backfill (backfill/) that re-derives points from image EXIF
in the `trips` S3 bucket, plus the private ingestion endpoint (ingest_router).
The SSR read router and scheduled jobs land in later phases of the migration off
the standalone trips.jomcgi.dev service.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register the private trips routers with the app.

    Only the authenticated ingestion router exists today; the SSR read router
    is wired in a later phase.
    """
    from trips.ingest_router import router as ingest_router

    app.include_router(ingest_router)
