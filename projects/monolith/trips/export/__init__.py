"""Daily static trip export. Only processed, metadata-free WebP leaves R2.

The manifest is the commit point: immutable JSON and all five derivatives must
exist before it is replaced. A failed run leaves the previous snapshot usable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote

PRESETS = ("thumb", "gallery", "preview", "display", "full")
# Leave at least half of the 20,000 Workers asset slots for HTML and other data.
PHOTO_ASSET_LIMIT = 10_000
PHOTO_SNAPSHOT_BYTES = 1024**3
WORKERS_ASSET_BYTES = 25 * 1024**2
PRIVATE_BUCKET = "stars"
PUBLIC_BUCKET = "factory-public"
PUBLIC_BASE = "https://factory-data.jomcgi.dev"
# The hub's ingest names originals img_<first 12 hex of SHA-256>.jpg; older
# imports used other names, such as img_000038.jpg. No "/", "." or "?" can
# reach the signed path or an R2 key.
IMAGE_KEY = re.compile(r"img_[A-Za-z0-9_-]{1,64}\.jpg\Z")
logger = logging.getLogger(__name__)


def encoded(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def slug_key(slug) -> str:
    """One R2 key component per slug, with no two slugs on the same key.

    Percent-encoding maps each character to itself (A-Z, a-z, 0-9, "_", "-")
    or to a "%"-led byte sequence, so it is injective; "." and "~" are
    encoded too, so no component is "." or "..". The JSON keeps the slug.
    """
    if not isinstance(slug, str) or not slug:
        raise ValueError("invalid trip slug")
    return quote(slug, safe="").replace(".", "%2E").replace("~", "%7E")


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
    """Fail closed on an original, malformed output, retained metadata, or a
    file with no decodable image frame."""
    if body[:4] != b"RIFF" or body[8:12] != b"WEBP":
        raise ValueError("imgproxy did not return WebP")
    if int.from_bytes(body[4:8], "little") + 8 != len(body):
        raise ValueError("truncated WebP")
    tags = []
    offset = 12
    while offset < len(body):
        tag = body[offset : offset + 4]
        if tag in (b"EXIF", b"XMP "):
            raise ValueError("imgproxy retained photo metadata")
        tags.append(tag)
        size = int.from_bytes(body[offset + 4 : offset + 8], "little")
        offset += 8 + size + size % 2
    if offset != len(body):
        raise ValueError("invalid WebP chunks")
    if not {b"VP8 ", b"VP8L"} & set(tags):
        raise ValueError("WebP has no image frame")
    from PIL import Image

    # Decode the frame, so a container with a broken bitstream is not stored.
    try:
        with Image.open(io.BytesIO(body)) as image:
            image.load()
            if image.format != "WEBP" or not all(image.size):
                raise ValueError("WebP has no image frame")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("WebP image frame does not decode") from exc


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
    index,
    trips,
    client,
    render,
    *,
    now=None,
    asset_limit=PHOTO_ASSET_LIMIT,
    byte_limit=PHOTO_SNAPSHOT_BYTES,
    allow_public=False,
):
    """Synchronous core; injectable S3 and renderer keep boundary tests local."""
    images = set()
    for row in index["trips"]:
        if row.get("default_image"):
            images.add(row["default_image"])
    for payload in trips:
        trip = payload["trip"]
        slug_key(trip["slug"])
        if trip.get("default_image"):
            images.add(trip["default_image"])
        images.update(p["image"] for p in payload["points"] if p.get("image"))
        images.update(h["image"] for h in trip["highlights"] if h.get("image"))
    if any(not IMAGE_KEY.fullmatch(key) for key in images):
        raise ValueError("trip export contains an image key outside monolith-trips")
    photo_bytes = 0
    photo_max_bytes = 0
    derivatives = []
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
            size = client.head_object(Bucket=PRIVATE_BUCKET, Key=key)["ContentLength"]
            photo_bytes += size
            photo_max_bytes = max(photo_max_bytes, size)
            derivatives.append(key)

    mode = (
        "snapshot"
        if (
            len(derivatives) <= asset_limit
            and photo_bytes <= byte_limit
            and photo_max_bytes <= WORKERS_ASSET_BYTES
        )
        else "public"
    )
    if mode == "public":
        if not allow_public:
            raise RuntimeError(
                "trip photos exceed snapshot limits; public publication is disabled"
            )
        for key in derivatives:
            if not exists(client, PUBLIC_BUCKET, key):
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
        "photoBytes": photo_bytes,
        "photoMaxBytes": photo_max_bytes,
        "photoBase": "/data/trips/photos"
        if mode == "snapshot"
        else f"{PUBLIC_BASE}/trips/photos",
        "images": sorted(images),
        "files": {},
    }

    def publish(name, payload):
        """Write the immutable copy and its alias; return the immutable key."""
        body = encoded(payload)
        immutable = f"trips/v/{name}-{hashlib.sha256(body).hexdigest()}.json"
        for key in (immutable, f"trips/{name}.json"):
            client.put_object(
                Bucket=PRIVATE_BUCKET,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
        return immutable

    # Trips live under trips/trip/, so no slug can name the index or the
    # manifest (trips/export.json).
    manifest["files"]["index"] = publish("index", index)
    manifest["files"]["trips"] = {
        p["trip"]["slug"]: publish(f"trip/{slug_key(p['trip']['slug'])}", p)
        for p in trips
    }
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

        export_trips(
            index,
            trips,
            client,
            render,
            allow_public=os.environ.get("TRIPS_EXPORT_PUBLIC_ENABLED", "false")
            == "true",
        )
