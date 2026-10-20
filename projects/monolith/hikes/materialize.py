"""Materialize the walk forecast snapshot for the Scotland connector.

hikes-refresh-forecasts calls materialize_handler after it stores new hours.
It writes one compact JSON object, ``walks.json`` in the stars R2 bucket, that
the connector (projects/scotland-connector) polls and serves from memory, the
same way it reads the stars snapshot. Walks live beside the stars objects
because the connector's read-only R2 token already reads that bucket.

The snapshot carries facts and links only: name, WalkHighlands URL, start
coordinates, distance, ascent and estimated duration, plus our own viable
weather hours. The scraped ``summary`` prose is deliberately left out; the
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
# version it does not know rather than misreading it.
SCHEMA_VERSION = 1


# One stored hour as _load selects it: (walk_uuid, hour_time, temp_c,
# precip_mm, wind_kmh, cloud_pct). Plain columns, not ORM objects: a refresh
# holds tens of thousands of hours.
HourRow = tuple[str, datetime, float, float, float, float]


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
    """The snapshot body: every publishable walk, with its future viable hours.

    Each hour is ``[unix_seconds, temp_c, precip_mm, wind_kmh, cloud_pct]``,
    the tuple hikes.forecast.compute_windows emits. A stored hour is one that
    passed the viability ladder (daylight, precipitation, wind), so presence
    means "good"; a missing daylight hour within the forecast means bad
    weather.
    """
    cutoff = top_of_hour(now)
    by_walk: dict[str, list[list[float]]] = {}
    for walk_uuid, at, temp_c, precip_mm, wind_kmh, cloud_pct in sorted(
        hours, key=lambda h: (h[0], as_utc(h[1]))
    ):
        hour_time = as_utc(at)
        if hour_time is None or hour_time < cutoff:
            continue
        by_walk.setdefault(walk_uuid, []).append(
            [int(hour_time.timestamp()), temp_c, precip_mm, wind_kmh, cloud_pct]
        )
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
                h.walk_uuid, h.hour_time, h.temp_c, h.precip_mm, h.wind_kmh, h.cloud_pct
            ).where(h.hour_time >= top_of_hour(now))
        ).all()
    )
    fetched_at = session.exec(select(func.max(models.WalkHour.fetched_at))).one()
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
