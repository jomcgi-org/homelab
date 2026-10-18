"""Ingest the light-pollution site grid from R2 into stars.sites (ADR 006).

An authorized offline step uploads ``grid.json`` (an array of site dicts) to R2.
Use the environment-only rclone setup in grimoire/tools/README.md, then:

    rclone --config /dev/null --s3-no-check-bucket copyto grid.json \\
        "R2:${STARS_GRID_S3_BUCKET:?}/${STARS_GRID_S3_KEY:?}"

The destination is chart stars.gridBucket / stars.gridKey (currently
stars/grid.json). The stars.load_grid scheduled job reads that object and
wholesale-replaces the stars.sites table so the refresh job has a current site
list to score.

The chart supplies the HTTPS R2 endpoint, AWS_REGION and the monolith-r2-s3
identity through the shared S3 environment. ``SEAWEEDFS_S3_ENDPOINT`` retains
its legacy name and has no default: an unset endpoint skips the job with a
warning. Upload and table replacement require separate authorization.

The network/S3 fetch runs in the async handler's worker thread; all Session I/O
uses a fresh ``Session(get_engine())`` inside that thread, mirroring stars.jobs.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from sqlmodel import Session, delete

from stars.grid_ingest import replace_grid
from stars.models import SiteMonthClimatology

logger = logging.getLogger("monolith.stars.grid")


def _s3_client():
    """Build a path-style S3 client for the configured R2 endpoint.

    Production uses the chart's monolith-r2-s3 credentials and region auto.
    Historical credential/region defaults and scheme-less HTTP normalization
    remain for legacy/test compatibility; they are not R2 configuration.
    """
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    # The chart supplies HTTPS for R2. Retain HTTP prefixing only for
    # scheme-less legacy/test endpoints; preserve explicit schemes.
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    # Scheme is guaranteed by the guard above; the inline nosemgrep clears the
    # pre-commit hook (boto3-endpoint-url-missing-scheme). Bare nosemgrep: this
    # line has no other rule matches.
    return boto3.client(  # nosemgrep: boto3-endpoint-url-missing-scheme
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "duckdb"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "duckdb"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )


def _fetch_grid() -> list[dict] | None:
    """Read grid.json from R2; None on missing endpoint or any error.

    A missing/empty ``SEAWEEDFS_S3_ENDPOINT`` is the expected state in
    environments without object storage (e.g. local dev): log a warning and no-op
    rather than crash. Any S3/parse error is also swallowed to a warning so a
    bad object never wedges the scheduler.
    """
    if not os.environ.get("SEAWEEDFS_S3_ENDPOINT", "").strip():
        logger.warning("stars.load_grid: SEAWEEDFS_S3_ENDPOINT unset, skipping")
        return None
    bucket = os.environ.get("STARS_GRID_S3_BUCKET", "stars")
    key = os.environ.get("STARS_GRID_S3_KEY", "stars/grid.json")
    try:
        s3 = _s3_client()
        obj = s3.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        data = json.loads(body)
    except Exception as exc:
        logger.warning("stars.load_grid: failed to fetch %s/%s: %s", bucket, key, exc)
        return None
    if not isinstance(data, list):
        logger.warning("stars.load_grid: grid.json is not a JSON array, skipping")
        return None
    return data


def _load_grid_sync() -> int:
    """Fetch the configured S3 grid and replace ``stars.sites`` with it."""
    grid = _fetch_grid()
    if not grid:
        return 0
    return replace_grid(grid)


async def load_grid_handler(session: Session) -> datetime | None:
    """Ingest the grid from R2 into stars.sites (scheduled job).

    The S3 fetch + DB write run together in a worker thread with their own fresh
    session; this handler never touches the passed session on the event loop.
    """
    import asyncio

    count = await asyncio.to_thread(_load_grid_sync)
    logger.info("stars.load_grid: %d sites", count)
    return None


def _fetch_climatology() -> list[dict] | None:
    """Read climatology.json from R2; None on missing endpoint or any error.

    The ERA5 backfill (ADR 009): an offline step uploads per-site, per-month-of-year
    sufficient stats out-of-band. A missing/empty ``SEAWEEDFS_S3_ENDPOINT`` is the
    expected state without configured object storage (e.g. local dev): log a warning
    and no-op rather than crash. Any S3/parse error is also swallowed to a warning
    so a bad object never wedges the scheduler.
    """
    if not os.environ.get("SEAWEEDFS_S3_ENDPOINT", "").strip():
        logger.warning("stars.load_climatology: SEAWEEDFS_S3_ENDPOINT unset, skipping")
        return None
    bucket = os.environ.get("STARS_GRID_S3_BUCKET", "stars")
    key = os.environ.get("STARS_CLIMATOLOGY_S3_KEY", "climatology.json")
    try:
        s3 = _s3_client()
        obj = s3.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        data = json.loads(body)
    except Exception as exc:
        logger.warning(
            "stars.load_climatology: failed to fetch %s/%s: %s", bucket, key, exc
        )
        return None
    if not isinstance(data, list):
        logger.warning(
            "stars.load_climatology: climatology.json is not a JSON array, skipping"
        )
        return None
    return data


def _load_climatology_sync() -> int:
    """Wholesale-replace stars.site_month_climatology from the backfill. Returns rows written.

    Malformed rows (missing site_id/month, non-numeric stats, or month outside
    1-12) are skipped with a logged count. The delete + add_all run in one
    transaction so a failure leaves the prior table intact rather than truncating
    it.
    """
    from core.db import get_engine

    backfill = _fetch_climatology()
    if not backfill:
        return 0

    rows: list[SiteMonthClimatology] = []
    skipped = 0
    for entry in backfill:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        site_id = entry.get("site_id")
        month = entry.get("month")
        if site_id is None or month is None:
            skipped += 1
            continue
        try:
            month_int = int(month)
            if not 1 <= month_int <= 12:
                skipped += 1
                continue
            rows.append(
                SiteMonthClimatology(
                    site_id=str(site_id),
                    month=month_int,
                    dark_hours=int(entry.get("dark_hours") or 0),
                    clear_dark_hours=int(entry.get("clear_dark_hours") or 0),
                )
            )
        except (TypeError, ValueError):
            skipped += 1
            continue
    if skipped:
        logger.warning(
            "stars.load_climatology: skipped %d malformed climatology rows", skipped
        )
    if not rows:
        logger.warning(
            "stars.load_climatology: no valid climatology rows, leaving table intact"
        )
        return 0

    with Session(get_engine()) as session:
        # synchronize_session=False: a wholesale clear before reload; there are
        # no in-session objects to keep in sync, so skip the ORM evaluator.
        session.execute(
            delete(SiteMonthClimatology).execution_options(synchronize_session=False)
        )
        session.add_all(rows)
        session.commit()
    return len(rows)


async def load_climatology_handler(session: Session) -> datetime | None:
    """Ingest the ERA5 climatology backfill into stars.site_month_climatology.

    The S3 fetch + DB write run together in a worker thread with their own fresh
    session; this handler never touches the passed session on the event loop.
    """
    import asyncio

    count = await asyncio.to_thread(_load_climatology_sync)
    logger.info("stars.load_climatology: %d rows", count)
    return None
