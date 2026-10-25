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
from stars.router import _DISPLAY_HOURS, _build_sites_from_db, get_history

logger = logging.getLogger("monolith.stars.materialize")


def _materialize_sync() -> int:
    """Build and atomically replace the compact S3 payload."""
    bucket = os.environ.get("STARS_GRID_S3_BUCKET", "stars")
    key = os.environ.get("STARS_SITES_S3_KEY", "sites.json")
    map_key = os.environ.get("STARS_MAP_S3_KEY", "map.json")
    history_key = os.environ.get("STARS_HISTORY_MAP_S3_KEY", "history-map.json")
    connector_key = os.environ.get("STARS_CONNECTOR_SITES_S3_KEY", "").strip()
    # Fail before reading or writing anything if activation aliases an existing
    # object, including the source grid/climatology and the walks snapshot.
    reserved = {
        key,
        map_key,
        history_key,
        os.environ.get("STARS_GRID_S3_KEY", "grid.json"),
        os.environ.get("STARS_CLIMATOLOGY_S3_KEY", "climatology.json"),
        os.environ.get("HIKES_WALKS_S3_KEY", "walks.json"),
    }
    if connector_key and connector_key in reserved:
        raise ValueError("STARS_CONNECTOR_SITES_S3_KEY must name a separate object")
    request = SimpleNamespace(headers={})
    response = Response()
    with Session(get_engine()) as session:
        payload = _build_sites_from_db(
            request,
            response,
            session,
            hour_limit=None if connector_key else _DISPLAY_HOURS,
        )
        history_payload = get_history(request, response, session)
    if not isinstance(payload, dict):
        raise RuntimeError("stars site builder returned a conditional response")
    connector_error = None
    if connector_key:
        # Serialize and release the uncapped bytes before constructing public
        # bodies. Trim the same tree in place, with no second forecast query or
        # duplicate site/hour dictionaries. Disabled runs never build this tree.
        connector_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            _s3_client().put_object(
                Bucket=bucket,
                Key=connector_key,
                Body=connector_body,
                ContentType="application/json",
                CacheControl="public, max-age=0, s-maxage=1800, stale-if-error=86400",
            )
            logger.info("stars connector materialized: %d bytes", len(connector_body))
        except Exception as exc:  # noqa: BLE001 - reported after public writes
            # SDK exceptions can retain the request body in attributes or
            # chained exceptions. Keep only text, including in log records.
            connector_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "stars connector publication failed; continuing public writes: %s",
                connector_error,
            )
        finally:
            del connector_body
        for site in payload["sites"]:
            del site["best_hours"][_DISPLAY_HOURS:]
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
    history_map_payload = {
        "sites": [
            {
                "id": site["id"],
                "name": site["name"],
                "lat": site["lat"],
                "lon": site["lon"],
                "clear": site["clear"],
                "dark": site["dark"],
            }
            for site in history_payload.get("sites", [])
        ]
    }
    history_map_body = json.dumps(history_map_payload, separators=(",", ":")).encode(
        "utf-8"
    )
    _s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        CacheControl="public, max-age=0, s-maxage=1800, stale-if-error=86400",
    )
    _s3_client().put_object(
        Bucket=bucket,
        Key=history_key,
        Body=history_map_body,
        ContentType="application/json",
        CacheControl="public, max-age=0, s-maxage=31536000, stale-if-error=604800",
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
    logger.info("stars history map materialized: %d bytes", len(history_map_body))
    if connector_error is not None:
        raise RuntimeError(f"stars connector publication failed: {connector_error}")
    return len(body)


async def materialize_handler(_session: Session) -> None:
    """Materialize stars in an off-pod Argo job."""
    await asyncio.to_thread(_materialize_sync)
