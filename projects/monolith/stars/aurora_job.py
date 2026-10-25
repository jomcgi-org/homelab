"""Bounded aurora snapshot job using the existing stars bucket and writer.

The public connector never performs upstream requests. MET bodies retain their
own collection clock across NOAA refreshes; provider Expires and Last-Modified
control revalidation. Missing terrain stays absent rather than a flat horizon.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

logger = logging.getLogger("monolith.stars.aurora")
NOAA_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
MET_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
USER_AGENT = "jomcgi-homelab-stars-aurora/1.0 https://jomcgi.dev"
MAX_SITES = 64
MAX_BODY = 2 << 20
MAX_CACHE = 16 << 20
CACHE_KEY = "aurora-provider-cache-v1.json"
TERRAIN_KEY = "aurora-terrain-v1.json"
SNAPSHOT_KEY = "aurora.json"
BINARY = "/usr/local/bin/stars-aurora"


def _utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("source clock must include timezone")
    return dt.astimezone(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def select_sites(rows: list[dict]) -> list[dict]:
    """One existing grid site per half-degree cell, capped deterministically.

    This is a bounded initial subset, not Scotland-wide coverage. Requests
    farther than the connector's existing 1 km site limit remain unknown.
    """
    cells: dict[tuple[int, int], dict] = {}
    for row in rows:
        lat, lon = row["lat"], row["lon"]
        if not (
            math.isfinite(lat)
            and math.isfinite(lon)
            and 54.5 <= lat <= 61
            and -9 <= lon <= -0.5
        ):
            continue
        key = (math.floor(lat * 2), math.floor(lon * 2))
        candidate = {"id": row["id"], "lat": round(lat, 4), "lon": round(lon, 4)}
        if key not in cells or candidate["id"] < cells[key]["id"]:
            cells[key] = candidate
    ordered = [cells[key] for key in sorted(cells)]
    if len(ordered) > MAX_SITES:
        ordered = [
            ordered[i * (len(ordered) - 1) // (MAX_SITES - 1)] for i in range(MAX_SITES)
        ]
    return ordered


def _load_sites() -> list[dict]:
    from core.db import get_engine
    from sqlmodel import Session, select

    from stars.models import Site

    with Session(get_engine()) as session:
        # Scalar metadata only, never the forecast history or ORM graph.
        rows = session.exec(select(Site.id, Site.lat, Site.lon)).all()
    return select_sites([{"id": sid, "lat": lat, "lon": lon} for sid, lat, lon in rows])


def read_object(s3, bucket: str, key: str) -> dict:
    from botocore.exceptions import ClientError

    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) in {"NoSuchKey", "404"}:
            return {}
        raise
    stream = obj["Body"]
    try:
        raw = stream.read(MAX_CACHE + 1)
    finally:
        stream.close()
    if len(raw) > MAX_CACHE:
        raise ValueError("aurora object exceeds limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("aurora object must be a mapping")
    return value


def _http_date(value: str | None) -> datetime | None:
    try:
        dt = parsedate_to_datetime(value or "")
        return dt.astimezone(UTC) if dt.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_provider(client, url: str, cached: dict, now, *, met: bool) -> dict:
    """Revalidate only after expiry; 304 proves the same provider representation.

    A 304 refreshes collection evidence, never the provider's issue/observation
    clock. A skipped request keeps collection evidence unchanged.
    """
    current = now()
    if cached:
        if not isinstance(cached.get("body"), dict):
            raise ValueError("invalid cached provider body")
        _utc(cached["collected_at"])
        expires = _utc(cached["expires_at"])
        if met and expires > current:
            return cached
    headers = {"User-Agent": USER_AGENT}
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]
    with client.stream("GET", url, headers=headers) as response:
        if response.status_code == 304:
            if not cached:
                raise ValueError("304 without cached provider body")
            body = cached["body"]
        else:
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                raw.extend(chunk)
                if len(raw) > MAX_BODY:
                    raise ValueError("provider body exceeds limit")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("provider body must be a mapping")
        collected = now()
        expires = _http_date(response.headers.get("Expires"))
        # MET has hourly updates. A missing/expired Expires never causes a
        # five-minute refetch loop. NOAA is independently refreshed each run.
        if expires is None or expires <= collected:
            expires = collected + timedelta(minutes=30 if met else 1)
        if expires > collected + timedelta(hours=24):
            raise ValueError("provider expiry exceeds one day")
        last_modified = response.headers.get(
            "Last-Modified", cached.get("last_modified", "")
        )
        if last_modified and _http_date(last_modified) is None:
            raise ValueError("invalid Last-Modified")
        return {
            "body": body,
            "collected_at": _iso(collected),
            "expires_at": _iso(expires),
            "last_modified": last_modified,
        }


def build_inputs(
    client, sites: list[dict], cache: dict, terrain: dict, now, sleep=time.sleep
) -> tuple[dict, dict]:
    if not 1 <= len(sites) <= MAX_SITES:
        raise ValueError("aurora site inventory outside limit")
    deadline = time.monotonic() + 180
    noaa = fetch_provider(client, NOAA_URL, cache.get("noaa", {}), now, met=False)
    fresh_cache = {"noaa": noaa, "weather": {}}
    weather = []
    for index, site in enumerate(sites):
        if time.monotonic() >= deadline:
            raise TimeoutError("aurora collection exceeded three minutes")
        identity = f"{site['id']}:{site['lat']}:{site['lon']}"
        prior = cache.get("weather", {}).get(identity, {})
        if index and (not prior or _utc(prior["expires_at"]) <= now()):
            sleep(1)  # At most one new MET request per second.
        url = str(httpx.URL(MET_URL, params={"lat": site["lat"], "lon": site["lon"]}))
        entry = fetch_provider(client, url, prior, now, met=True)
        fresh_cache["weather"][identity] = entry
        profile = terrain.get(site["id"])
        if profile and (
            profile.get("lat") != site["lat"] or profile.get("lon") != site["lon"]
        ):
            raise ValueError("terrain profile coordinates differ from weather site")
        weather.append(
            {
                **site,
                "collected_at": entry["collected_at"],
                "met": entry["body"],
                "terrain": profile.get("terrain") if profile else None,
            }
        )
    return {
        "collected_at": _iso(now()),
        "noaa_collected_at": noaa["collected_at"],
        "noaa": noaa["body"],
        "weather": weather,
    }, fresh_cache


def materialize(inputs: dict) -> bytes:
    # Execute the same strict Go materializer used in connector tests. No
    # duplicated scoring/validation rules and no hand-authored derived verdicts.
    with tempfile.TemporaryDirectory(prefix="stars-aurora-") as directory:
        source = Path(directory) / "inputs.json"
        source.write_text(json.dumps(inputs, separators=(",", ":")))
        if source.stat().st_size > MAX_CACHE:
            raise ValueError("aurora inputs exceed limit")
        result = subprocess.run(
            [BINARY, "--input", str(source)],
            check=True,
            capture_output=True,
            timeout=30,
        )
    if not 0 < len(result.stdout) <= MAX_CACHE:
        raise ValueError("materialized snapshot outside limit")
    return result.stdout


def publish(s3, bucket: str, inputs: dict, cache: dict, render=materialize) -> int:
    # Validate first. A malformed upstream never overwrites the last good
    # snapshot or poisons the persisted provider cache.
    body = render(inputs)
    cache_body = json.dumps(cache, separators=(",", ":")).encode()
    if len(cache_body) > MAX_CACHE:
        raise ValueError("provider cache exceeds limit")
    s3.put_object(
        Bucket=bucket,
        Key=CACHE_KEY,
        Body=cache_body,
        ContentType="application/json",
        CacheControl="no-store",
    )
    s3.put_object(
        Bucket=bucket,
        Key=SNAPSHOT_KEY,
        Body=body,
        ContentType="application/json",
        CacheControl="no-store",
    )
    logger.info(
        "aurora materialized sites=%d terrain_sites=%d bytes=%d sha256=%s",
        len(inputs["weather"]),
        sum(p["terrain"] is not None for p in inputs["weather"]),
        len(body),
        hashlib.sha256(body).hexdigest(),
    )
    return len(body)


def run() -> int:
    from stars.grid import _s3_client

    for name in ("SEAWEEDFS_S3_ENDPOINT", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        if not os.environ.get(name):
            raise RuntimeError(f"{name} is required; no legacy credential fallback")
    sites = _load_sites()
    s3 = _s3_client()
    bucket = os.environ.get("STARS_GRID_S3_BUCKET", "stars")
    cache = read_object(s3, bucket, CACHE_KEY)
    terrain = read_object(s3, bucket, TERRAIN_KEY)
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        inputs, cache = build_inputs(
            client, sites, cache, terrain, lambda: datetime.now(UTC)
        )
    return publish(s3, bucket, inputs, cache)


async def aurora_handler(_session) -> None:
    await asyncio.to_thread(run)
