"""Daily static trip export. Only processed, metadata-free WebP leaves R2.

The manifest is the commit point: immutable JSON and all five derivatives must
exist before it is replaced. A failed run leaves the previous snapshot usable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone

PRESETS = ("thumb", "gallery", "preview", "display", "full")
# Leave at least half of the 20,000 Workers asset slots for HTML and other data.
PHOTO_ASSET_LIMIT = 10_000
PRIVATE_BUCKET = "stars"
PUBLIC_BUCKET = "factory-public"
PUBLIC_BASE = "https://factory-data.jomcgi.dev"
IMAGE_KEY = re.compile(r"img_[a-f0-9]{12,64}\.jpg\Z")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
logger = logging.getLogger(__name__)


def encoded(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def signed_path(key: str, preset: str, secret: str, salt: str) -> str:
    if not IMAGE_KEY.fullmatch(key) or preset not in PRESETS:
        raise ValueError("invalid trip image or preset")
    path = f"/{preset}/plain/s3://monolith-trips/{key}"
    digest = hmac.new(
        bytes.fromhex(secret), bytes.fromhex(salt) + path.encode(), hashlib.sha256
    ).digest()
    signature = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"/{signature}{path}"


def validate_webp(body: bytes) -> None:
    """Fail closed on an original, malformed output, or retained metadata."""
    if body[:4] != b"RIFF" or body[8:12] != b"WEBP":
        raise ValueError("imgproxy did not return WebP")
    if int.from_bytes(body[4:8], "little") + 8 != len(body):
        raise ValueError("truncated WebP")
    offset = 12
    while offset < len(body):
        tag = body[offset : offset + 4]
        if tag in (b"EXIF", b"XMP "):
            raise ValueError("imgproxy retained photo metadata")
        size = int.from_bytes(body[offset + 4 : offset + 8], "little")
        offset += 8 + size + size % 2
    if offset != len(body):
        raise ValueError("invalid WebP chunks")


def exists(client, bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey"):
            raise
        return False


def export_trips(
    index, trips, client, render, *, now=None, asset_limit=PHOTO_ASSET_LIMIT
):
    """Synchronous core; injectable S3 and renderer keep boundary tests local."""
    images = set()
    for row in index["trips"]:
        if row.get("default_image"):
            images.add(row["default_image"])
    for payload in trips:
        trip = payload["trip"]
        if trip["slug"] == "index" or not SLUG.fullmatch(trip["slug"]):
            raise ValueError("invalid trip slug")
        if trip.get("default_image"):
            images.add(trip["default_image"])
        images.update(p["image"] for p in payload["points"] if p.get("image"))
        images.update(h["image"] for h in trip["highlights"] if h.get("image"))
    if any(not IMAGE_KEY.fullmatch(key) for key in images):
        raise ValueError("trip export contains a non-content-addressed image")
    mode = "snapshot" if len(images) * len(PRESETS) <= asset_limit else "public"
    for image in sorted(images):
        for preset in PRESETS:
            key = f"trips/photos/{image}/{preset}.webp"
            if not exists(client, PRIVATE_BUCKET, key):
                body = render(image, preset)
                validate_webp(body)
                client.put_object(
                    Bucket=PRIVATE_BUCKET,
                    Key=key,
                    Body=body,
                    ContentType="image/webp",
                    CacheControl="public, max-age=31536000, immutable",
                )
            if mode == "public" and not exists(client, PUBLIC_BUCKET, key):
                # The only permitted source is the validated derivative prefix.
                client.copy_object(
                    Bucket=PUBLIC_BUCKET,
                    Key=key,
                    CopySource={"Bucket": PRIVATE_BUCKET, "Key": key},
                )

    manifest = {
        "asOf": (now or datetime.now(timezone.utc)).isoformat(),
        "refreshSeconds": 86400,
        "photoCount": len(images),
        "photoAssetCount": len(images) * len(PRESETS),
        "photoMode": mode,
        "photoBase": "/data/trips/photos"
        if mode == "snapshot"
        else f"{PUBLIC_BASE}/trips/photos",
        "images": sorted(images),
        "files": {},
    }
    for name, payload in [("index", index)] + [(p["trip"]["slug"], p) for p in trips]:
        body = encoded(payload)
        immutable = f"trips/v/{name}-{hashlib.sha256(body).hexdigest()}.json"
        client.put_object(
            Bucket=PRIVATE_BUCKET,
            Key=immutable,
            Body=body,
            ContentType="application/json",
        )
        client.put_object(
            Bucket=PRIVATE_BUCKET,
            Key=f"trips/{name}.json",
            Body=body,
            ContentType="application/json",
        )
        manifest["files"][name] = immutable
    client.put_object(
        Bucket=PRIVATE_BUCKET,
        Key="trips/export.json",
        Body=encoded(manifest),
        ContentType="application/json",
    )
    logger.info(
        "trips export: %d photos, %d WebP assets, %s",
        len(images),
        len(images) * len(PRESETS),
        mode,
    )
    return manifest


def run() -> None:
    import httpx
    from fastapi import Response
    from sqlmodel import Session

    from core.db import get_engine
    from core.s3 import path_style_client
    from trips.read_router import get_trip, list_trips

    # The same read serializers as the API; no originals or signing URLs enter JSON.
    with Session(get_engine()) as session:
        index = list_trips(Response(), session)
        trips = [get_trip(row["slug"], Response(), session) for row in index["trips"]]
    client = path_style_client(os.environ["SEAWEEDFS_S3_ENDPOINT"])
    secret, salt = os.environ["IMGPROXY_KEY"], os.environ["IMGPROXY_SALT"]
    base = os.environ["TRIPS_IMGPROXY_URL"].rstrip("/")
    with httpx.Client(timeout=120, follow_redirects=False) as http:

        def render(key, preset):
            response = http.get(base + signed_path(key, preset, secret, salt))
            if response.status_code != 200:
                # Never log signed URLs or response bodies.
                raise RuntimeError(
                    f"trip image render failed: HTTP {response.status_code}"
                )
            return response.content

        export_trips(index, trips, client, render)
