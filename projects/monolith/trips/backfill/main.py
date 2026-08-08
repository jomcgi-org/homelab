"""Local backfill CLI: re-derive trips Postgres rows from S3 image EXIF.

Run by hand against a port-forwarded SeaweedFS + Postgres, e.g.:

    bazel run //projects/monolith:trips_backfill -- run \\
        --slug 2025-liard-hot-springs \\
        --config projects/trips/frontend/public/trips/2025-liard-hot-springs/config.yaml \\
        --endpoint http://localhost:8333 \\
        --database-url postgresql://postgres@localhost:5432/monolith \\
        --kml van-to-kamloops.kml --kml-start 2025-01-03T10:28:00

It lists the source `trips` bucket, extracts GPS/timestamp/optics EXIF from each
image, optionally adds route-only gap points from KML directions and elevation
from the NRCan CDEM API, then replaces trips.points for the trip (and upserts
the trips.trips metadata row from config.yaml). It also copies the image bytes
server-side into the serving bucket (`monolith-trips`) the read path reads from.
Idempotent: re-running rebuilds the rows and skips already-copied images.
"""

import asyncio
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import boto3
import httpx
import typer
import yaml
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import delete
from sqlmodel import Session, create_engine

from trips import exif, transform
from trips.models import Trip, TripPoint

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("monolith.trips.backfill")

app = typer.Typer(help="Backfill the trips Postgres schema from S3 image EXIF.")

DEFAULT_BUCKET = "trips"
DEFAULT_DEST_BUCKET = "monolith-trips"
ELEVATION_API = "https://geogratis.gc.ca/services/elevation/cdem/altitude"
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}

# The trips migration once wrote 67-byte S3 error bodies ("operation Lookup
# failed") as "images". Defend the backfill copy too: a real JPEG opens with the
# SOI marker and is KB-MB; anything below this floor or without the magic is an
# error body, not a photo.
_MIN_IMAGE_BYTES = 1024
_JPEG_MAGIC = b"\xff\xd8"


def _looks_like_jpeg(data: bytes) -> bool:
    """True if ``data`` is plausibly a real JPEG (SOI magic + size floor)."""
    return len(data) >= _MIN_IMAGE_BYTES and data.startswith(_JPEG_MAGIC)


def make_engine(database_url: str):
    """Create an engine, rewriting the scheme for psycopg v3 (see app/db.py)."""
    url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url)


def get_s3_client(endpoint: str):
    """S3 client for SeaweedFS (auth disabled in-cluster)."""
    # Guard against a scheme-less endpoint (semgrep boto3-endpoint-url-missing-scheme).
    if not endpoint.startswith(("http://", "https://")):
        endpoint = f"http://{endpoint}"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id="any",
        aws_secret_access_key="any",
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def list_image_keys(s3, bucket: str) -> list[str]:
    """List image object keys in the bucket (paginated)."""
    keys: list[str] = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            if Path(obj["Key"]).suffix.lower() in _IMAGE_EXTS:
                keys.append(obj["Key"])
        if not resp.get("IsTruncated"):
            return keys
        token = resp["NextContinuationToken"]


def _copy_images(
    s3, src_bucket: str, dest_bucket: str, keys: list[str], concurrency: int = 10
) -> tuple[int, int]:
    """Server-side copy each key from the source bucket into the serving bucket.

    SeaweedFS serves both buckets off the same S3 endpoint, so this is a
    server-side ``copy_object`` (no bytes through this process). A ``head_object``
    skip-guard on the destination makes reruns idempotent: an object already
    present in ``dest_bucket`` is not re-copied. Returns ``(copied, skipped)``.
    A no-op when ``src_bucket == dest_bucket`` (nothing to move).
    """
    if src_bucket == dest_bucket:
        logger.info(
            "source bucket == dest bucket (%s); skipping image copy", src_bucket
        )
        return 0, 0

    def work(key: str) -> str:
        try:
            s3.head_object(Bucket=dest_bucket, Key=key)
            return "skipped"  # already in the serving bucket
        except ClientError as exc:
            # 404/NoSuchKey just means it is not in the dest yet, so copy it.
            # Anything else (auth, transport) is a real error worth surfacing.
            code = exc.response.get("Error", {}).get("Code")
            if code not in ("404", "NoSuchKey", "NotFound"):
                raise
        s3.copy_object(
            Bucket=dest_bucket,
            Key=key,
            CopySource={"Bucket": src_bucket, "Key": key},
        )
        # copy_object moves bytes server-side, so we never see them here; a
        # post-copy HEAD is the cheapest way to catch a tiny error-body object
        # that slipped into the source bucket. Warn only: a bad object is
        # already filtered out of the DB rows by _extract_photo_points, so its
        # presence in the serving bucket is harmless and not worth failing on.
        try:
            head = s3.head_object(Bucket=dest_bucket, Key=key)
            size = head.get("ContentLength", 0)
            if size < _MIN_IMAGE_BYTES:
                logger.warning(
                    "copied %s is only %d bytes; likely corrupt or an error body",
                    key,
                    size,
                )
        except ClientError as exc:  # noqa: BLE001 - sanity check, never fatal
            logger.warning("post-copy HEAD failed for %s: %s", key, exc)
        return "copied"

    copied = skipped = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for future in as_completed(pool.submit(work, k) for k in keys):
            if future.result() == "copied":
                copied += 1
            else:
                skipped += 1
    logger.info(
        "image copy s3://%s -> s3://%s: %d copied, %d skipped",
        src_bucket,
        dest_bucket,
        copied,
        skipped,
    )
    return copied, skipped


def _extract_photo_points(
    s3, bucket: str, keys: list[str], tmp_dir: Path, concurrency: int
) -> list[dict]:
    """Download images, pull EXIF, return photo point dicts (GPS only).

    The intermediate shape allows GPS-less photos to be skipped and coordinates
    to be deduplicated before the batched elevation lookup.
    """

    def work(key: str):
        local = tmp_dir / Path(key).name
        try:
            s3.download_file(bucket, key, str(local))
            head = local.read_bytes()
            if not _looks_like_jpeg(head):
                logger.warning(
                    "skipping %s: not a valid JPEG (%d bytes); likely an error body",
                    key,
                    len(head),
                )
                return key, None, None, None, None
            lat, lng, ts, optics = exif.extract_exif(local)
            return key, lat, lng, ts, optics
        except Exception as exc:  # noqa: BLE001 - skip unreadable objects
            logger.warning("failed to process %s: %s", key, exc)
            return key, None, None, None, None
        finally:
            local.unlink(missing_ok=True)

    points: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for future in as_completed(pool.submit(work, k) for k in keys):
            key, lat, lng, ts, optics = future.result()
            if lat is None or lng is None:
                continue  # no GPS -> not a map point
            if not transform.is_valid_coordinates(lat, lng):
                continue
            points.append(
                {
                    "key": key,
                    "lat": round(lat, 5),
                    "lng": round(lng, 5),
                    "timestamp": ts,
                    "optics": optics,
                }
            )
    return points


async def _fetch_elevations(
    coords: list[tuple[float, float]], concurrency: int
) -> dict[tuple[float, float], float | None]:
    """Fetch NRCan CDEM elevations, deduped by rounded coordinate."""
    unique = sorted({(round(la, 5), round(ln, 5)) for la, ln in coords})
    sem = asyncio.Semaphore(concurrency)
    out: dict[tuple[float, float], float | None] = {}

    async with httpx.AsyncClient(timeout=10.0) as client:

        async def one(lat: float, lng: float):
            async with sem:
                try:
                    resp = await client.get(
                        ELEVATION_API, params={"lat": lat, "lon": lng}
                    )
                    out[(lat, lng)] = (
                        resp.json().get("altitude") if resp.status_code == 200 else None
                    )
                except Exception as exc:  # noqa: BLE001 - best effort, leave as None
                    logger.debug("elevation fetch failed for %s,%s: %s", lat, lng, exc)
                    out[(lat, lng)] = None

        await asyncio.gather(*(one(la, ln) for la, ln in unique))
    return out


def _load_trip_row(slug: str, config_path: Path, tz: str) -> Trip:
    """Build the trips.trips row from a frontend config.yaml."""
    data = yaml.safe_load(config_path.read_text()) or {}
    trip = data.get("trip", {})
    timeline = data.get("timeline", {})
    days = {str(k): v for k, v in (data.get("days") or {}).items()}
    return Trip(
        slug=slug,
        title=trip.get("title", slug),
        short_title=trip.get("short_title"),
        subtitle=trip.get("subtitle"),
        tz=tz,
        default_image=timeline.get("default_image"),
        default_zoom=timeline.get("default_zoom"),
        days=days,
        highlights=data.get("highlights") or [],
        stats=data.get("stats") or {},
    )


@app.command()
def run(
    slug: Annotated[str, typer.Option(help="Trip slug, e.g. 2025-liard-hot-springs")],
    config: Annotated[
        Path | None,
        typer.Option(help="frontend config.yaml to upsert the trips.trips row"),
    ] = None,
    bucket: Annotated[str, typer.Option(help="Source S3 bucket")] = DEFAULT_BUCKET,
    dest_bucket: Annotated[
        str, typer.Option(help="Serving S3 bucket images are copied into")
    ] = DEFAULT_DEST_BUCKET,
    copy_images: Annotated[
        bool,
        typer.Option(
            "--copy-images/--no-copy-images",
            help="Copy image bytes from --bucket into --dest-bucket",
        ),
    ] = True,
    endpoint: Annotated[
        str, typer.Option(help="SeaweedFS S3 endpoint")
    ] = "http://localhost:8333",
    database_url: Annotated[
        str,
        typer.Option(
            envvar="DATABASE_URL", help="Postgres URL (defaults to $DATABASE_URL)"
        ),
    ] = "postgresql://postgres@localhost:5432/monolith",
    source: Annotated[str, typer.Option(help="Image source tag")] = "gopro",
    tz: Annotated[
        str, typer.Option("--timezone", help="IANA zone for EXIF timestamps")
    ] = "America/Vancouver",
    kml: Annotated[
        list[Path] | None, typer.Option(help="KML route file(s) for gap points")
    ] = None,
    kml_start: Annotated[
        list[str] | None,
        typer.Option(help="ISO start time per --kml (parallel order)"),
    ] = None,
    kml_max_points: Annotated[
        int, typer.Option(help="Max gap points sampled per KML")
    ] = 100,
    elevation: Annotated[
        bool, typer.Option(help="Fetch elevation from NRCan CDEM")
    ] = True,
    concurrency: Annotated[int, typer.Option(help="Parallel downloads/requests")] = 10,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", "-n", help="Compute only, don't write")
    ] = False,
) -> None:
    """Rebuild trips.points (and optionally trips.trips) for one trip."""
    now = datetime.now(timezone.utc)
    s3 = get_s3_client(endpoint)

    print(f"Listing s3://{bucket} ...")
    keys = list_image_keys(s3, bucket)
    print(f"Found {len(keys)} images")

    with tempfile.TemporaryDirectory(prefix="trips-backfill-") as tmp:
        photo = _extract_photo_points(s3, bucket, keys, Path(tmp), concurrency)
    print(f"Extracted {len(photo)} photo points with GPS")

    # Route-only gap points from KML directions.
    gap: list[dict] = []
    kml_files = list(kml or [])
    starts = list(kml_start or [])
    if kml_files and len(starts) != len(kml_files):
        raise typer.BadParameter("--kml-start must be given once per --kml")
    for path, start in zip(kml_files, starts):
        coords = transform.parse_kml_coordinates(path.read_text())
        coords = transform.sample_coordinates(coords, kml_max_points)
        gap.extend(transform.gap_points(coords, datetime.fromisoformat(start), tz))
    if gap:
        print(f"Built {len(gap)} gap points from {len(kml_files)} KML route(s)")

    # Elevation enrichment (photo + gap).
    elevations: dict[tuple[float, float], float | None] = {}
    if elevation:
        coords = [(p["lat"], p["lng"]) for p in photo] + [
            (p["lat"], p["lng"]) for p in gap
        ]
        print(f"Fetching elevation for {len(coords)} points ...")
        elevations = asyncio.run(_fetch_elevations(coords, concurrency))
        resolved = sum(1 for v in elevations.values() if v is not None)
        print(f"Elevation: {resolved}/{len(elevations)} unique coords resolved")

    rows: list[TripPoint] = []
    for p in photo:
        o: exif.Optics | None = p["optics"]
        rows.append(
            TripPoint(
                trip_slug=slug,
                id=transform.point_id_from_image_key(p["key"]),
                lat=p["lat"],
                lng=p["lng"],
                taken_at=transform.localize(p["timestamp"], tz, now),
                image=p["key"],
                source=source,
                tags=[],
                elevation=elevations.get((p["lat"], p["lng"])),
                light_value=o.light_value if o else None,
                iso=o.iso if o else None,
                shutter_speed=o.shutter_speed if o else None,
                aperture=o.aperture if o else None,
                focal_length_35mm=o.focal_length_35mm if o else None,
            )
        )
    for g in gap:
        rows.append(
            TripPoint(
                trip_slug=slug,
                id=g["id"],
                lat=g["lat"],
                lng=g["lng"],
                taken_at=g["taken_at"],
                image=None,
                source=g["source"],
                tags=g["tags"],
                elevation=elevations.get((g["lat"], g["lng"])),
            )
        )

    print(f"Prepared {len(rows)} total points for trip {slug}")
    if dry_run:
        print("[DRY RUN] no database writes")
        return

    engine = make_engine(database_url)
    with Session(engine) as session:
        if config is not None:
            session.merge(_load_trip_row(slug, config, tz))
        session.execute(delete(TripPoint).where(TripPoint.trip_slug == slug))
        session.add_all(rows)
        session.commit()
    print(f"Wrote {len(rows)} points for {slug}")

    # Move the source bytes into the bucket the serving read path reads from.
    # Done after the DB write (dry-run returns earlier, so it never copies); the
    # order is not load-bearing since copy is idempotent and points reference
    # keys by name, not by presence in the serving bucket.
    if copy_images:
        print(f"Copying {len(keys)} images s3://{bucket} -> s3://{dest_bucket} ...")
        copied, skipped = _copy_images(s3, bucket, dest_bucket, keys, concurrency)
        print(f"Image copy: {copied} copied, {skipped} skipped")
    else:
        print("Image copy disabled (--no-copy-images)")


if __name__ == "__main__":
    app()
