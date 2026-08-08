"""Materialize the stars forecast payload for the public read path."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from types import SimpleNamespace

from fastapi import Response
from sqlmodel import Session

from core.db import get_engine
from stars.grid import _s3_client
from stars.router import _build_sites_from_db

logger = logging.getLogger("monolith.stars.materialize")


def _materialize_sync() -> int:
    """Build and atomically replace the compact S3 payload."""
    bucket = os.environ.get("STARS_GRID_S3_BUCKET", "stars")
    key = os.environ.get("STARS_SITES_S3_KEY", "sites.json")
    map_key = os.environ.get("STARS_MAP_S3_KEY", "map.json")
    request = SimpleNamespace(headers={})
    response = Response()
    with Session(get_engine()) as session:
        payload = _build_sites_from_db(request, response, session)
    if not isinstance(payload, dict):
        raise RuntimeError("stars site builder returned a conditional response")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    map_payload = {
        "count": payload.get("count", 0),
        "darkness": payload.get("darkness", "astronomical"),
        "fetched_at": payload.get("fetched_at"),
        "sites": [
            {
                "id": site["id"],
                "name": site["name"],
                "lat": site["lat"],
                "lon": site["lon"],
                "clear_dark_hours": site["clear_dark_hours"],
                "clear_twilight_hours": site["clear_twilight_hours"],
            }
            for site in payload.get("sites", [])
        ],
    }
    map_body = json.dumps(map_payload, separators=(",", ":")).encode("utf-8")
    _s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        CacheControl="public, max-age=0, s-maxage=1800, stale-if-error=86400",
    )
    _s3_client().put_object(
        Bucket=bucket,
        Key=map_key,
        Body=map_body,
        ContentType="application/json",
        CacheControl="public, max-age=0, s-maxage=1800, stale-if-error=86400",
    )
    logger.info(
        "stars materialized: %d sites, %d bytes, s3://%s/%s",
        payload.get("count", 0),
        len(body),
        bucket,
        key,
    )
    logger.info(
        "stars map materialized: %d bytes, s3://%s/%s", len(map_body), bucket, map_key
    )
    return len(body)


async def materialize_handler(_session: Session) -> None:
    """Materialize stars in an off-pod Argo job."""
    await asyncio.to_thread(_materialize_sync)
