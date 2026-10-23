"""Materialize the walk forecast snapshot for the Scotland connector.

hikes-refresh-forecasts calls materialize_handler after it stores new hours.
It writes one compact JSON object, ``walks.json`` in the stars R2 bucket, that
the connector (projects/scotland-connector) polls and serves from memory, the
same way it reads the stars snapshot. Walks live beside the stars objects
because the connector's read-only R2 token already reads that bucket.

The snapshot carries facts and links only: name, WalkHighlands URL, start
coordinates, distance, ascent and estimated duration, plus every daylight
forecast hour at the start, good or not, so the connector can judge an hour
by the caller's own limits. The scraped ``summary`` prose is deliberately left out; the
connector links to WalkHighlands for the route itself (#7252).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from core.clock import as_utc, iso
from shared.forecast_freshness import top_of_hour
from sqlmodel import Session, func, select

from hikes import models

logger = logging.getLogger("monolith.hikes")

# Bump when the payload shape changes incompatibly; the connector refuses a
# version it does not know rather than misreading it. v2: every daylight hour
# (v1 held viable hours only), each hour with fog and low cloud.
SCHEMA_VERSION = 2

# The stored columns each hour carries after its unix time, in wire order:
# hikes.forecast.HOUR_FIELDS without the time.
_HOUR_COLUMNS = (
    "temp_c",
    "precip_mm",
    "wind_kmh",
    "cloud_pct",
    "fog_pct",
    "low_cloud_pct",
)

# One stored hour as _load selects it: (walk_uuid, hour_time, then
# _HOUR_COLUMNS). Plain columns keep it light: a refresh holds tens of
# thousands of hours.
HourRow = tuple


def _publishable(w: models.Walk) -> bool:
    """A walk the connector can rank: named, linked and with a real duration.

    The scrape can store a 0 h duration (the prod seed has one); the doable
    rule needs a positive one, so such a walk is left out rather than
    reported as doable in any good hour.
    """
    return bool(w.uuid and w.name and w.url) and w.duration_h > 0


def build_payload(
    walks: list[models.Walk],
    hours: list[HourRow],
    fetched_at: datetime | None,
    now: datetime,
) -> dict:
    """The snapshot body: every publishable walk, with its future daylight hours.

    Each hour is ``[unix_seconds, *_HOUR_COLUMNS]``, the hikes.forecast
    HOUR_FIELDS tuple, with null for a value met.no left out or a row stored
    before its column existed. Every daylight hour is present, good or not:
    the connector applies the viability rule (or the caller's limits) itself.
    A walk whose last fetch failed keeps its earlier rows, so until it next
    fetches it may hold only the good hours stored before this version.
    """
    cutoff = top_of_hour(now)
    by_walk: dict[str, list[list[float]]] = {}
    for walk_uuid, at, *values in sorted(hours, key=lambda h: (h[0], as_utc(h[1]))):
        hour_time = as_utc(at)
        if hour_time is None or hour_time < cutoff:
            continue
        by_walk.setdefault(walk_uuid, []).append([int(hour_time.timestamp()), *values])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso(now),
        "fetched_at": iso(fetched_at),
        "walks": [
            {
                "id": w.uuid,
                "name": w.name,
                "url": w.url,
                "lat": w.latitude,
                "lon": w.longitude,
                "distance_km": w.distance_km,
                "ascent_m": w.ascent_m,
                "duration_h": w.duration_h,
                "hours": by_walk.get(w.uuid, []),
                "forecast_fetched_at": iso(w.forecast_fetched_at),
            }
            for w in sorted(walks, key=lambda w: w.uuid)
            if _publishable(w)
        ],
    }


def _load(session: Session, now: datetime) -> dict:
    walks = list(session.exec(select(models.Walk)).all())
    h = models.WalkHour
    hours = list(
        session.exec(
            select(
                h.walk_uuid,
                h.hour_time,
                *(getattr(h, name) for name in _HOUR_COLUMNS),
            ).where(h.hour_time >= top_of_hour(now))
        ).all()
    )
    fetched_at = session.exec(select(func.max(models.Walk.forecast_fetched_at))).one()
    return build_payload(walks, hours, as_utc(fetched_at), now)


def _s3_client():
    """A path-style S3 client for the configured R2 endpoint."""
    import boto3
    from botocore.config import Config

    # The chart supplies an https:// R2 endpoint for .s3 jobs; materialize_sync
    # refuses to run without one, so the scheme is always present here.
    return boto3.client(  # nosemgrep: boto3-endpoint-url-missing-scheme
        "s3",
        endpoint_url=os.environ["SEAWEEDFS_S3_ENDPOINT"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_REGION", "auto"),
        config=Config(s3={"addressing_style": "path"}),
    )


def materialize_sync(now: datetime | None = None) -> int | None:
    """Build and replace walks.json. Returns bytes written, None when skipped.

    Without an S3 endpoint (local dev, tests, a job not marked ``s3: true``)
    this logs and does nothing, so the forecast refresh never depends on it.
    """
    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "").strip()
    if not endpoint.startswith("https://"):
        logger.warning("hikes materialize: no https S3 endpoint set, skipping")
        return None
    bucket = os.environ.get("STARS_GRID_S3_BUCKET", "stars")
    key = os.environ.get("HIKES_WALKS_S3_KEY", "walks.json")

    from core.db import get_engine

    with Session(get_engine()) as session:
        payload = _load(session, now or datetime.now(timezone.utc))
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    _s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        CacheControl="no-store",
    )
    logger.info(
        "hikes materialized: %d walks, %d bytes, s3://%s/%s",
        len(payload["walks"]),
        len(body),
        bucket,
        key,
    )
    return len(body)


async def materialize_handler() -> None:
    """Materialize off the event loop (the work is DB reads and one PUT)."""
    await asyncio.to_thread(materialize_sync)
