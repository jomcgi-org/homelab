"""Bounded SRWR daily extract to stars/roadworks.json. No database state."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import os
import re
import time
import zipfile
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

SOURCE = "https://downloads.srwr.scot/disruptions-export/daily/"
MAX_DOWNLOAD = 16 << 20
MAX_EXPANDED = 64 << 20
MAX_OUTPUT = 4 << 20
MAX_ROWS = 100_000
MAX_ACTIVITIES = 5000
ACTIVE = {
    "Advance Planning",
    "Planned",
    "Proposed",
    "Recorded",
    "Commenced",
    "In Progress",
}
EXCLUDED = {
    "Emergency",
    "Urgent",
    "Special Urgent",
    "Remedial Dangerous",
    "Plant Information Request",
}
FIELDS = set(
    "Version ActivityReference PhaseNumber Category TrafficManagement ActivityStatus StartDateTimeUTC EndDateTimeUTC LastUpdatedDateTimeUTC Street Latitude Longitude GeometryCentroid Easting Northing UniqueStreetReferenceNumber Description Location".split()
)


def utc(value: str) -> datetime:
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None or d.utcoffset() != timedelta(0):
        raise ValueError("SRWR clock must be UTC")
    return d


def iso(d: datetime) -> str:
    return d.astimezone(UTC).isoformat().replace("+00:00", "Z")


def text(value: str, limit: int) -> str:
    if len(value) > limit:
        raise ValueError("SRWR text exceeds contract")
    return " ".join(value.split())


def materialize(raw: bytes, source_modified: datetime, collected: datetime) -> bytes:
    if source_modified > collected or collected - source_modified >= timedelta(
        hours=36
    ):
        raise ValueError("SRWR source clock outside freshness window")
    if len(raw) > MAX_DOWNLOAD:
        raise ValueError("SRWR download exceeds limit")
    activities = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) > 8 or sum(m.file_size for m in members) > MAX_EXPANDED:
            raise ValueError("SRWR expanded archive exceeds limit")
        targets = [m for m in members if m.filename == "CurrentActivities.csv"]
        if len(targets) != 1:
            raise ValueError("SRWR requires one CurrentActivities.csv")
        # Read only the named member, never extract archive paths. CRC is checked.
        with archive.open(targets[0]) as stream:
            body = stream.read(MAX_EXPANDED + 1)
        if len(body) > MAX_EXPANDED:
            raise ValueError("SRWR member exceeds limit")
    # UTF-8 with optional BOM, quoted commas/newlines, CRLF, and large WKT cells.
    # csv's field limit is process-global; this runs in its own bounded job pod.
    csv.field_size_limit(4 << 20)
    reader = csv.DictReader(
        io.StringIO(body.decode("utf-8-sig"), newline=""), strict=True
    )
    headers = reader.fieldnames or []
    if not FIELDS.issubset(headers) or len(headers) != len(set(headers)):
        raise ValueError("unsupported SRWR headers")
    for n, row in enumerate(reader, 1):
        if n > MAX_ROWS or None in row or any(v is None for v in row.values()):
            raise ValueError("SRWR rows exceed limit or are malformed")
        if row["Version"] != "00":
            raise ValueError("unsupported SRWR record version")
        control = row["TrafficManagement"]
        street = text(row["Street"], 500)
        if control not in {"Road Closure", "Convoy Working"}:
            continue
        if row["ActivityStatus"] not in ACTIVE or row["Category"] in EXCLUDED:
            continue
        if not (
            re.match(r"^A\d{1,4}(?:[tT])?\b", street, re.I)
            or re.search(r"\btrunk road\b", street, re.I)
        ):
            continue
        lat, lon = float(row["Latitude"]), float(row["Longitude"])
        e, north = float(row["Easting"]), float(row["Northing"])
        point = re.fullmatch(
            r"POINT\s*\(\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s*\)", row["GeometryCentroid"]
        )
        if not (
            all(math.isfinite(v) for v in (lat, lon, e, north))
            and 54.5 <= lat <= 61
            and -9 <= lon <= -0.5
            and 0 <= e <= 500_000
            and 500_000 <= north <= 1_300_000
            and point
        ):
            raise ValueError("invalid SRWR plotted geometry")
        if abs(float(point[1]) - e) > 1 or abs(float(point[2]) - north) > 1:
            raise ValueError("inconsistent SRWR centroid")
        start, end, updated = (
            utc(row[k])
            for k in ("StartDateTimeUTC", "EndDateTimeUTC", "LastUpdatedDateTimeUTC")
        )
        if (
            start.astimezone(ZoneInfo("Europe/London")).date()
            > end.astimezone(ZoneInfo("Europe/London")).date()
            or updated > collected
        ):
            raise ValueError("invalid SRWR date range")
        ref = text(row["ActivityReference"], 100)
        phase, usrn = row["PhaseNumber"], row["UniqueStreetReferenceNumber"]
        if not ref or not phase.isdigit() or not usrn.isdigit():
            raise ValueError("invalid SRWR identity")
        key = f"{ref}:{phase}:{usrn}:{lat}:{lon}"
        activity = dict(
            id=key,
            reference=ref,
            phase=phase,
            road=street,
            control=control,
            status=row["ActivityStatus"],
            lat=lat,
            lon=lon,
            start=iso(start),
            end=iso(end),
            updated_at=iso(updated),
            timing=text(row["Description"], 4000),
            location=text(row["Location"], 500),
        )
        if key in activities and activities[key] != activity:
            raise ValueError("conflicting SRWR duplicate")
        activities[key] = activity
        if len(activities) > MAX_ACTIVITIES:
            raise ValueError("SRWR filtered activities exceed limit")
    # Empty is a valid filtered feed, unlike exceptions which never publish.
    snapshot = dict(
        schema_version=1,
        source_url=SOURCE,
        source_modified_at=iso(source_modified),
        collected_at=iso(collected),
        activities=[activities[k] for k in sorted(activities)],
    )
    body = json.dumps(
        snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(body) > MAX_OUTPUT:
        raise ValueError("SRWR snapshot exceeds limit")
    return body


def download(client, now, monotonic=time.monotonic) -> tuple[bytes, datetime]:
    """Follow at most four public HTTPS redirects without exposing SAS URLs."""
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    url, deadline = SOURCE, monotonic() + 90
    try:
        for _ in range(5):
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username
                or parts.password
            ):
                raise ValueError("invalid redirect")
            with client.stream("GET", url) as response:
                if monotonic() >= deadline:
                    raise ValueError("download deadline")
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = str(response.url.join(response.headers["Location"]))
                    continue
                if response.status_code != 200:
                    raise ValueError("download status")
                modified = parsedate_to_datetime(response.headers["Last-Modified"])
                if modified.tzinfo is None or modified > now():
                    raise ValueError("source clock")
                raw = bytearray()
                for chunk in response.iter_bytes(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_DOWNLOAD or monotonic() >= deadline:
                        raise ValueError("download limit")
                return bytes(raw), modified.astimezone(UTC)
        raise ValueError("redirect limit")
    except Exception:
        # httpx exceptions and response objects may contain the signed URL.
        raise ValueError(
            "SRWR public download failed (details suppressed to protect signed redirects)"
        ) from None


def run() -> int:
    if os.environ.get("ROADWORKS_ENABLED", "false") != "true":
        return 0
    from stars.grid import _s3_client

    for name in ("SEAWEEDFS_S3_ENDPOINT", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        if not os.environ.get(name):
            raise RuntimeError(f"{name} is required")
    # HTTP client request logging is disabled: redirects contain temporary SAS tokens.
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        raw, modified = download(client, lambda: datetime.now(UTC))
    return publish(
        _s3_client(),
        os.environ.get("STARS_GRID_S3_BUCKET", "stars"),
        raw,
        modified,
        datetime.now(UTC),
    )


def publish(
    s3, bucket: str, raw: bytes, modified: datetime, collected: datetime
) -> int:
    body = materialize(raw, modified, collected)
    # The single write happens only after complete validation; failures preserve
    # the old object and both clocks. No auxiliary cache to partially replace.
    s3.put_object(
        Bucket=bucket,
        Key="roadworks.json",
        Body=body,
        ContentType="application/json",
        CacheControl="no-store",
    )
    return len(body)


async def roadworks_handler(_session) -> None:
    await asyncio.to_thread(run)
