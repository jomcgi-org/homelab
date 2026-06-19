"""trips: road-trip photo journeys, served SSR at /app/trips.

This package holds the Postgres-backed data model (models.py), a local,
run-by-hand backfill (backfill/) that re-derives points from image EXIF in the
`trips` S3 bucket, the private ingestion endpoint (ingest_router), and the
SSR read router (read_router) that backs the /app/trips pages.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register the private trips routers with the app.

    The private app serves both the authenticated ingestion router and the
    read router.
    """
    from trips.ingest_router import router as ingest_router
    from trips.read_router import router as read_router

    app.include_router(ingest_router)
    app.include_router(read_router)


def register_public(app: FastAPI) -> None:
    """Trips public surface is the read router only.

    The write path (ingest/s3/exif/transform) stays out of the public import
    closure; see app/main_public_imports_test.py.
    """
    from trips.read_router import router as read_router

    app.include_router(read_router)
