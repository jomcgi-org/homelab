"""Bounded checkpoint, immutable object publication, then exact-version deletion.

All entry points are synchronous and run in the prune worker thread. Sessions
are closed before storage calls. See ARCHITECTURE.md, Stars elapsed archive.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from core.clock import as_utc, iso
from shared.forecast_freshness import top_of_hour
from sqlalchemy import func
from sqlmodel import Session, delete, select, update

from stars.models import HourArchiveCheckpoint, Site, SiteHour

MAX_ROWS = 1024
# Stop starting batches with 60s left in the unchanged 300s workflow deadline.
DRAIN_SECONDS = 240
CHUNK_ROWS = 128
CHUNK_BYTES = 131072
MAX_CHUNKS = 8
STATE_BYTES = 1048576
ROW_BYTES = 8192
TEXT_BYTES = 1024
FIELDS = (
    "site_id",
    "hour_time",
    "cloud_area_fraction",
    "air_temperature",
    "dew_spread",
    "sun_elevation_deg",
    "relative_humidity",
    "wind_speed",
    "symbol",
    "fetched_at",
)
SITE_FIELDS = ("id", "name", "lat", "lon", "altitude_m", "lp_zone", "source")


def encoded(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def configured_destination() -> tuple[str, str]:
    """Do not let the legacy client's fallback credentials mask missing config."""
    required = (
        "SEAWEEDFS_S3_ENDPOINT",
        "AWS_REGION",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "STARS_ARCHIVE_BUCKET",
        "STARS_ARCHIVE_PREFIX",
    )
    if any(not os.environ.get(name) for name in required):
        raise ValueError("enabled stars archive requires storage configuration")
    prefix = os.environ["STARS_ARCHIVE_PREFIX"].strip("/")
    if not prefix or len(prefix) > 200:
        raise ValueError("invalid stars archive prefix")
    return os.environ["STARS_ARCHIVE_BUCKET"], prefix


def _bounded_columns(model, names):
    # Bound database-to-client text allocation even for a corrupt oversized row.
    # Four UTF-8 bytes per character; the row encoder enforces the byte limit.
    return [
        func.substr(getattr(model, name), 1, TEXT_BYTES + 1)
        if name in {"site_id", "symbol", "id", "name", "lp_zone", "source"}
        else getattr(model, name)
        for name in names
    ]


def checkpoint(engine, bucket: str, prefix: str, now: datetime) -> dict | None:
    """Commit the replay bytes before any remote I/O; CAS chooses one worker."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    cutoff = top_of_hour(as_utc(now))
    with Session(engine) as session:
        session.execute(
            insert(HourArchiveCheckpoint).values(id=1).on_conflict_do_nothing()
        )
        session.commit()
    with Session(engine) as session:
        saved = session.get(HourArchiveCheckpoint, 1)
        if saved.payload is not None:
            if len(saved.payload.encode()) > STATE_BYTES:
                raise ValueError("archive checkpoint exceeds limit")
            return json.loads(saved.payload)
        envelope = {
            "version": 1,
            "batch_id": uuid4().hex,
            "archived_at": iso(now),
            "cutoff": iso(cutoff),
            "bucket": bucket,
            "prefix": prefix,
            "chunks": [],
        }
        # Stream a server-limited, stable selection. No OFFSET or grid list.
        rows = session.exec(
            select(
                *_bounded_columns(SiteHour, FIELDS),
                *_bounded_columns(Site, SITE_FIELDS),
            )
            .outerjoin(Site, Site.id == SiteHour.site_id)
            .where(SiteHour.hour_time < cutoff)
            .order_by(SiteHour.hour_time, SiteHour.site_id)
            .limit(MAX_ROWS)
            .execution_options(yield_per=CHUNK_ROWS)
        )
        current = []
        chunk_bytes = 2  # JSON array brackets; each later row adds one comma.
        row_bytes = 0
        for values in rows:
            hour = dict(zip(FIELDS, values[: len(FIELDS)]))
            site = dict(zip(SITE_FIELDS, values[len(FIELDS) :]))
            if any(
                isinstance(v, str) and len(v.encode()) > TEXT_BYTES
                for v in (*hour.values(), *site.values())
            ):
                raise ValueError("archive row text exceeds limit; no deletion")
            for field in ("hour_time", "fetched_at"):
                hour[field] = iso(hour[field])
            row = {"site": site, "hour": hour}
            size = len(encoded(row))
            if size > ROW_BYTES:
                raise ValueError("archive row exceeds limit; no deletion")
            if row_bytes + size > STATE_BYTES - 16384:
                break
            row_bytes += size
            if (
                len(current) == CHUNK_ROWS
                or chunk_bytes + bool(current) + size > CHUNK_BYTES
            ):
                envelope["chunks"].append(current)
                current = []
                chunk_bytes = 2
                if len(envelope["chunks"]) == MAX_CHUNKS:
                    break
            chunk_bytes += bool(current) + size
            current.append(row)
        if current:
            envelope["chunks"].append(current)
        if not envelope["chunks"]:
            return None
        payload = encoded(envelope).decode()
        if len(payload.encode()) > STATE_BYTES:
            raise ValueError("archive checkpoint exceeds limit; no deletion")
        claimed = session.execute(
            update(HourArchiveCheckpoint)
            .where(
                HourArchiveCheckpoint.id == 1, HourArchiveCheckpoint.payload.is_(None)
            )
            .values(payload=payload)
        ).rowcount
        session.commit()
        if claimed != 1:
            raise RuntimeError("archive checkpoint claimed concurrently; retry")
        return envelope


def immutable_verified(client, bucket: str, key: str, body: bytes) -> None:
    """Atomic create, bounded content readback, including uncertain PUT results.

    R2 documents atomic If-None-Match on PutObject. The pinned botocore service
    model must expose IfNoneMatch; no check-then-unconditional-put fallback.
    """
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except (BotoCoreError, ClientError, OSError):
        # A timeout may follow a committed write. GET must prove identical bytes.
        # Missing/unreadable/conflicting data raises and preserves the checkpoint.
        logging.getLogger(__name__).debug(
            "Conditional archive PUT needs readback reconciliation"
        )
    response = client.get_object(Bucket=bucket, Key=key)
    stream = response["Body"]
    try:
        actual = stream.read(len(body) + 1)
    finally:
        stream.close()
    if actual != body or digest(actual) != digest(body):
        raise ValueError("archive object content conflict or verification mismatch")


def deletion_statement(rows):
    """One statement per version: at most ten bind parameters, no IN list."""
    for row in rows:
        values = dict(row["hour"])
        for field in ("hour_time", "fetched_at"):
            values[field] = datetime.fromisoformat(values[field])
        yield (
            delete(SiteHour)
            .where(*(getattr(SiteHour, name) == values[name] for name in FIELDS))
            .execution_options(synchronize_session=False)
        )


def run_archive(engine, client, bucket: str, prefix: str, now=None) -> dict:
    batch = checkpoint(engine, bucket, prefix, now or datetime.now(UTC))
    if batch is None:
        return {"deleted": 0, "rows": 0, "backlog": False}
    # A configuration change cannot redirect an unfinished batch.
    if (batch["bucket"], batch["prefix"]) != (bucket, prefix):
        raise ValueError("unfinished archive destination differs from configuration")
    base = f"{prefix}/v1/{batch['batch_id']}"
    metadata = {k: batch[k] for k in ("version", "batch_id", "archived_at", "cutoff")}
    chunks = []
    for index, rows in enumerate(batch["chunks"]):
        body = encoded({**metadata, "index": index, "rows": rows})
        if len(body) > CHUNK_BYTES + ROW_BYTES:
            raise ValueError("archive chunk exceeds limit")
        key = f"{base}/{index:04d}-{digest(body)}.json"
        immutable_verified(client, bucket, key, body)
        chunks.append(
            {"key": key, "sha256": digest(body), "bytes": len(body), "rows": len(rows)}
        )
    marker = encoded(
        {**metadata, "chunks": chunks, "rows": sum(c["rows"] for c in chunks)}
    )
    if len(marker) > ROW_BYTES:
        raise ValueError("archive completion record exceeds limit")
    immutable_verified(client, bucket, f"{base}/complete.json", marker)
    deleted = 0
    for rows in batch["chunks"]:
        with Session(engine) as session:
            for statement in deletion_statement(rows):
                deleted += session.execute(statement).rowcount or 0
            session.commit()
    with Session(engine) as session:
        session.execute(
            update(HourArchiveCheckpoint)
            .where(
                HourArchiveCheckpoint.id == 1,
                HourArchiveCheckpoint.payload == encoded(batch).decode(),
            )
            .values(payload=None)
        )
        backlog = (
            session.exec(
                select(SiteHour.site_id)
                .where(SiteHour.hour_time < datetime.fromisoformat(batch["cutoff"]))
                .limit(1)
            ).first()
            is not None
        )
        session.commit()
    return {
        "deleted": deleted,
        "rows": sum(c["rows"] for c in chunks),
        "backlog": backlog,
        "batch_id": batch["batch_id"],
    }


def drain_archive(engine, client, bucket: str, prefix: str, now=None) -> int:
    """Complete bounded batches at a fixed cutoff until empty or visibly fail.

    The budget is checked between batches. A slow in-flight operation remains
    subject to the workflow deadline; its durable checkpoint is replayable.
    """
    deadline = monotonic() + DRAIN_SECONDS
    now = now or datetime.now(UTC)
    deleted = 0
    batches = 0
    while monotonic() < deadline:
        result = run_archive(engine, client, bucket, prefix, now)
        deleted += result["deleted"]
        # A replay can report no backlog below an older cutoff. Probe the fixed
        # invocation cutoff with another selection before declaring it drained.
        if result["rows"] == 0:
            return deleted
        batches += 1
        logging.getLogger(__name__).info("stars archive bounded batch: %s", result)
    raise RuntimeError(
        f"stars archive drain budget exhausted after {batches} batches and "
        f"{deleted} deletions; elapsed backlog has not been confirmed empty"
    )


def run_configured_archive(engine) -> int:
    from stars.grid import _s3_client

    bucket, prefix = configured_destination()
    client = _s3_client()
    if (
        "IfNoneMatch"
        not in client.meta.service_model.operation_model(
            "PutObject"
        ).input_shape.members
    ):
        raise RuntimeError("installed S3 client lacks atomic conditional PutObject")
    return drain_archive(engine, client, bucket, prefix)
