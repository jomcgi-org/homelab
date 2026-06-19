"""
Publish Trip Images

Scans a directory (e.g., SD card) for images and POSTs the raw JPEG bytes to the
monolith trip-image ingestion endpoint. The server does EXIF extraction,
content-addressing, the S3 upload, and the Postgres write; this client is a thin
uploader whose only durability layer is a local SQLite queue (retry on failure).

Auth: the endpoint is protected by Cloudflare Access at the Envoy gateway. When
reached remotely, ``CF-Access-Client-Id`` / ``CF-Access-Client-Secret``
service-token headers are sent; at home (kubectl port-forward) those are omitted.
"""

import asyncio
import json
import logging
import math
import os
import signal
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated

import httpx
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeRemainingColumn,
)
import typer

# Defaults
DB_PATH = Path(__file__).parent / "publish_queue.db"

# Ingestion endpoint config (read at call time so tests/env can override).
INGEST_PATH = "/api/trips/ingest"

# Namespace UUID for deterministic local dedup-key generation. The server
# content-addresses by sha256 of the bytes; this key is only the UNIQUE column
# the local queue uses to avoid re-enqueuing the same source image.
IMAGE_KEY_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# Per-request timeout for the upload POST. Images are a few MB, so allow plenty.
HTTP_TIMEOUT = httpx.Timeout(120.0)

logger = logging.getLogger(__name__)

app = typer.Typer(help="Publish trip images to the monolith ingestion endpoint")


class UploadStatus(str, Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    # Permanent skip: the server rejected the image as un-ingestable (no GPS).
    # Retrying would never succeed, so it is dequeued without being completed.
    SKIPPED = "skipped"


@dataclass
class OpticsData:
    """Camera exposure data from EXIF."""

    light_value: float | None = (
        None  # Exposure Value (EV) - e.g., 8.6 for dim conditions
    )
    iso: int | None = None  # ISO sensitivity - e.g., 393
    shutter_speed: str | None = None  # Shutter speed as string - e.g., "1/240"
    aperture: float | None = None  # F-number - e.g., 2.5
    focal_length_35mm: int | None = None  # Focal length in 35mm equivalent - e.g., 16


@dataclass
class ImageRecord:
    id: int
    source_path: str
    dest_key: str
    status: UploadStatus
    retry_count: int
    error_message: str | None
    lat: float | None
    lng: float | None
    timestamp: str | None
    created_at: str
    completed_at: str | None
    tags: list[str] | None = None  # User-defined tags (e.g., "hotspring", "wildlife")
    # OPTICS - Camera exposure data
    optics: OpticsData | None = None


class UploadQueue:
    """Persistent queue for tracking image uploads."""

    MAX_RETRIES = 3

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL,
                    dest_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    lat REAL,
                    lng REAL,
                    timestamp TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    tags TEXT,
                    light_value REAL,
                    iso INTEGER,
                    shutter_speed TEXT,
                    aperture REAL,
                    focal_length_35mm INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON images(status)")
            # Migrations: add columns if they don't exist (existing field DBs).
            migrations = [
                "ALTER TABLE images ADD COLUMN tags TEXT",
                "ALTER TABLE images ADD COLUMN light_value REAL",
                "ALTER TABLE images ADD COLUMN iso INTEGER",
                "ALTER TABLE images ADD COLUMN shutter_speed TEXT",
                "ALTER TABLE images ADD COLUMN aperture REAL",
                "ALTER TABLE images ADD COLUMN focal_length_35mm INTEGER",
            ]
            for migration in migrations:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # Column already exists
            conn.commit()

    def add(
        self,
        source_path: Path,
        dest_key: str,
        lat: float | None,
        lng: float | None,
        timestamp: str | None,
        tags: list[str] | None = None,
        optics: OpticsData | None = None,
    ) -> int | None:
        """Add image to queue. Returns ID or None if already exists."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO images (source_path, dest_key, lat, lng, timestamp, status, created_at, tags,
                                       light_value, iso, shutter_speed, aperture, focal_length_35mm)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(source_path),
                        dest_key,
                        lat,
                        lng,
                        timestamp,
                        UploadStatus.PENDING.value,
                        datetime.now().isoformat(),
                        json.dumps(tags) if tags else None,
                        optics.light_value if optics else None,
                        optics.iso if optics else None,
                        optics.shutter_speed if optics else None,
                        optics.aperture if optics else None,
                        optics.focal_length_35mm if optics else None,
                    ),
                )
                conn.commit()
                return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None

    def get_pending(self) -> list[ImageRecord]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM images
                WHERE status = ? OR (status = ? AND retry_count < ?)
                ORDER BY id ASC
                """,
                (
                    UploadStatus.PENDING.value,
                    UploadStatus.FAILED.value,
                    self.MAX_RETRIES,
                ),
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def get_completed(self) -> list[ImageRecord]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM images WHERE status = ? ORDER BY id ASC",
                (UploadStatus.COMPLETED.value,),
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def mark_uploading(self, record_id: int) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE images SET status = ? WHERE id = ?",
                (UploadStatus.UPLOADING.value, record_id),
            )
            conn.commit()

    def mark_completed(self, record_id: int) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE images SET status = ?, completed_at = ? WHERE id = ?",
                (UploadStatus.COMPLETED.value, datetime.now().isoformat(), record_id),
            )
            conn.commit()

    def mark_skipped(self, record_id: int, reason: str) -> None:
        """Permanently dequeue a record the server will never accept (e.g. no GPS)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE images SET status = ?, error_message = ?, completed_at = ? WHERE id = ?",
                (
                    UploadStatus.SKIPPED.value,
                    reason,
                    datetime.now().isoformat(),
                    record_id,
                ),
            )
            conn.commit()

    def mark_failed(self, record_id: int, error: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE images
                SET status = ?, error_message = ?, retry_count = retry_count + 1
                WHERE id = ?
                """,
                (UploadStatus.FAILED.value, error, record_id),
            )
            conn.commit()

    def reset_uploading(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE images SET status = ? WHERE status = ?",
                (UploadStatus.PENDING.value, UploadStatus.UPLOADING.value),
            )
            conn.commit()
            return cursor.rowcount

    def get_stats(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM images GROUP BY status"
            ).fetchall()
            return {row[0]: row[1] for row in rows}

    def _row_to_record(self, row: sqlite3.Row) -> ImageRecord:
        tags_json = row["tags"] if "tags" in row.keys() else None
        keys = row.keys()

        # Build OpticsData if any optics field is present
        optics = None
        if any(
            col in keys
            for col in [
                "light_value",
                "iso",
                "shutter_speed",
                "aperture",
                "focal_length_35mm",
            ]
        ):
            optics = OpticsData(
                light_value=row["light_value"] if "light_value" in keys else None,
                iso=row["iso"] if "iso" in keys else None,
                shutter_speed=row["shutter_speed"] if "shutter_speed" in keys else None,
                aperture=row["aperture"] if "aperture" in keys else None,
                focal_length_35mm=row["focal_length_35mm"]
                if "focal_length_35mm" in keys
                else None,
            )
            # Only keep optics if at least one field is non-None
            if not any(
                [
                    optics.light_value,
                    optics.iso,
                    optics.shutter_speed,
                    optics.aperture,
                    optics.focal_length_35mm,
                ]
            ):
                optics = None

        return ImageRecord(
            id=row["id"],
            source_path=row["source_path"],
            dest_key=row["dest_key"],
            status=UploadStatus(row["status"]),
            retry_count=row["retry_count"],
            error_message=row["error_message"],
            lat=row["lat"],
            lng=row["lng"],
            timestamp=row["timestamp"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            tags=json.loads(tags_json) if tags_json else None,
            optics=optics,
        )


class GracefulShutdown:
    """Handle graceful shutdown on SIGINT/SIGTERM."""

    def __init__(self):
        self.shutdown_requested = False
        self._original_sigint = None
        self._original_sigterm = None

    def __enter__(self):
        self._original_sigint = signal.signal(signal.SIGINT, self._handler)
        self._original_sigterm = signal.signal(signal.SIGTERM, self._handler)
        return self

    def __exit__(self, *args):
        signal.signal(signal.SIGINT, self._original_sigint)
        signal.signal(signal.SIGTERM, self._original_sigterm)

    def _handler(self, signum, frame):
        if self.shutdown_requested:
            print("\nForce quit - exiting immediately")
            raise SystemExit(1)
        print("\nShutdown requested - finishing current upload...")
        self.shutdown_requested = True


def get_gps_info(exif_data: dict) -> dict:
    """Extract GPS info from EXIF data."""
    gps_info = {}
    for key, val in exif_data.items():
        tag = GPSTAGS.get(key, key)
        gps_info[tag] = val
    return gps_info


def dms_to_decimal(dms: tuple, ref: str) -> float:
    """Convert GPS coordinates from DMS to decimal degrees."""
    degrees, minutes, seconds = dms
    decimal = float(degrees) + float(minutes) / 60 + float(seconds) / 3600
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def calculate_light_value(
    aperture: float | None, shutter_time: float | None, iso: int | None
) -> float | None:
    """Calculate Light Value (EV) from exposure triangle.

    LV = log2(N²/t) - log2(ISO/100)
    where N is aperture (f-number), t is shutter time in seconds
    """
    if aperture is None or shutter_time is None or iso is None:
        return None
    if aperture <= 0 or shutter_time <= 0 or iso <= 0:
        return None

    try:
        lv = math.log2((aperture**2) / shutter_time) - math.log2(iso / 100)
        return round(lv, 1)
    except (ValueError, ZeroDivisionError):
        return None


def format_shutter_speed(exposure_time: float | None) -> str | None:
    """Format exposure time as readable shutter speed string.

    E.g., 0.00416666... -> "1/240"
    """
    if exposure_time is None or exposure_time <= 0:
        return None

    if exposure_time >= 1:
        return f"{exposure_time:.1f}s"
    else:
        # Express as fraction 1/x
        denominator = round(1 / exposure_time)
        return f"1/{denominator}"


def extract_exif(
    image_path: Path,
) -> tuple[float | None, float | None, str | None, OpticsData | None]:
    """Extract GPS coordinates, timestamp, and OPTICS data from EXIF.

    GPS + timestamp drive local time-interval sampling and the dedup key, so
    they stay. The server re-extracts EXIF on ingest, so the optics fields here
    are informational only (kept in the local queue, never sent).
    # TODO(trips): server now extracts EXIF; the optics extraction below is no
    # longer used by the upload path and could be removed in a later cleanup.
    """
    try:
        img = Image.open(image_path)
        exif_data = img._getexif()

        if not exif_data:
            return None, None, None, None

        lat = None
        lng = None
        timestamp = None

        # Build tag name lookup
        exif = {}
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            exif[tag] = value

        # Extract GPS
        if "GPSInfo" in exif:
            gps = get_gps_info(exif["GPSInfo"])
            if "GPSLatitude" in gps and "GPSLongitude" in gps:
                lat = dms_to_decimal(gps["GPSLatitude"], gps.get("GPSLatitudeRef", "N"))
                lng = dms_to_decimal(
                    gps["GPSLongitude"], gps.get("GPSLongitudeRef", "E")
                )

        # Extract timestamp (EXIF time is camera local time, not UTC)
        # Store without timezone suffix - frontend will display in Pacific
        if "DateTimeOriginal" in exif:
            dt = datetime.strptime(exif["DateTimeOriginal"], "%Y:%m:%d %H:%M:%S")
            timestamp = dt.isoformat()
        elif "DateTime" in exif:
            dt = datetime.strptime(exif["DateTime"], "%Y:%m:%d %H:%M:%S")
            timestamp = dt.isoformat()

        # Extract OPTICS data
        optics = OpticsData()

        # ISO - ISOSpeedRatings (can be tuple or int)
        iso_raw = exif.get("ISOSpeedRatings")
        if iso_raw:
            optics.iso = int(iso_raw[0] if isinstance(iso_raw, tuple) else iso_raw)

        # Aperture - FNumber (stored as Ratio)
        fnumber = exif.get("FNumber")
        if fnumber:
            optics.aperture = round(float(fnumber), 1)

        # Shutter speed - ExposureTime (stored as Ratio)
        exposure_time = exif.get("ExposureTime")
        exposure_time_float = None
        if exposure_time:
            exposure_time_float = float(exposure_time)
            optics.shutter_speed = format_shutter_speed(exposure_time_float)

        # Focal length 35mm equivalent - FocalLengthIn35mmFilm
        focal_35mm = exif.get("FocalLengthIn35mmFilm")
        if focal_35mm:
            optics.focal_length_35mm = int(focal_35mm)

        # Calculate Light Value from exposure triangle
        optics.light_value = calculate_light_value(
            optics.aperture, exposure_time_float, optics.iso
        )

        # Only return optics if we have at least some data
        has_optics = any(
            [
                optics.iso,
                optics.aperture,
                optics.shutter_speed,
                optics.focal_length_35mm,
            ]
        )

        return lat, lng, timestamp, optics if has_optics else None

    except Exception as e:
        logger.warning("Could not extract EXIF from %s: %s", image_path.name, e)
        print(f"  Warning: Could not extract EXIF from {image_path.name}: {e}")
        return None, None, None, None


def scan_images(source_dir: Path) -> list[Path]:
    """Scan directory for image files (recursive)."""
    extensions = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
    images = []

    for path in source_dir.rglob("*"):
        # Skip macOS resource fork files
        if path.name.startswith("._"):
            continue
        if path.is_file() and path.suffix.lower() in extensions:
            images.append(path)

    # Sort by file modification time (preserved from camera/SD card)
    return sorted(images, key=lambda p: p.stat().st_mtime)


def sample_images_by_time(
    images: list[Path], interval_seconds: int
) -> list[tuple[Path, float | None, float | None, str | None, OpticsData | None]]:
    """Sample images to have at least one per interval (in seconds).

    Returns list of (path, lat, lng, timestamp, optics) tuples to avoid re-extracting EXIF later.
    Images without valid timestamps are included if no image was selected in the current window.
    """
    if interval_seconds <= 0:
        # No sampling - return all with EXIF data
        return [(img, *extract_exif(img)) for img in images]

    selected: list[
        tuple[Path, float | None, float | None, str | None, OpticsData | None]
    ] = []
    last_selected_time: datetime | None = None

    for img_path in images:
        lat, lng, timestamp, optics = extract_exif(img_path)

        # Parse timestamp if available
        img_time: datetime | None = None
        if timestamp:
            try:
                img_time = datetime.fromisoformat(timestamp)
            except ValueError:
                pass

        # Selection logic:
        # 1. Always take the first image
        # 2. Take image if we don't have a valid timestamp for comparison
        # 3. Take image if enough time has passed since last selected
        should_select = False

        if not selected:
            # First image - always take it
            should_select = True
        elif last_selected_time is None:
            # Last selected had no timestamp - take this one if it has a timestamp
            # or if we've gone through several images without selecting
            should_select = img_time is not None
        elif img_time is None:
            # Current image has no timestamp - skip it (prefer images with timestamps)
            should_select = False
        else:
            # Both have timestamps - check interval
            elapsed = (img_time - last_selected_time).total_seconds()
            should_select = elapsed >= interval_seconds

        if should_select:
            selected.append((img_path, lat, lng, timestamp, optics))
            last_selected_time = img_time

    return selected


def generate_dest_key(image_path: Path, source: str, timestamp: str | None) -> str:
    """Generate a deterministic local dedup key for the queue.

    Uses UUID5 (deterministic) based on:
    - source (gopro, camera, phone)
    - EXIF timestamp (if available)
    - original filename (as fallback/disambiguation)

    This is only the local queue's UNIQUE key (so the same image is not enqueued
    twice across re-runs). The server stores the image under its own
    content-addressed key derived from the bytes.
    """
    # Build identity string: source + timestamp + filename
    # Timestamp is primary identifier, filename disambiguates same-second shots
    identity_parts = [source]
    if timestamp:
        identity_parts.append(timestamp)
    identity_parts.append(image_path.name)

    identity = ":".join(identity_parts)

    # Generate deterministic UUID from identity
    key_uuid = uuid.uuid5(IMAGE_KEY_NAMESPACE, identity)

    ext = image_path.suffix.lower()
    if ext in (".heic", ".heif"):
        ext = ".jpg"  # Will need conversion

    return f"img_{key_uuid.hex[:12]}{ext}"


def ingest_config() -> tuple[str, dict[str, str]]:
    """Build the ingestion base URL and request headers from the environment.

    - ``TRIPS_INGEST_URL`` (required): base origin, e.g. ``https://private.jomcgi.dev``.
    - ``CF_ACCESS_CLIENT_ID`` / ``CF_ACCESS_CLIENT_SECRET`` (optional): when both
      are set, the Cloudflare Access service-token headers are added (remote
      path). Omitted for the local kubectl port-forward path.
    """
    base = os.getenv("TRIPS_INGEST_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("TRIPS_INGEST_URL is not set")

    headers: dict[str, str] = {}

    cf_id = os.getenv("CF_ACCESS_CLIENT_ID")
    cf_secret = os.getenv("CF_ACCESS_CLIENT_SECRET")
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret

    return base, headers


async def post_image(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    record: ImageRecord,
    trip: str,
    source: str,
) -> httpx.Response:
    """POST one image's raw bytes to the ingestion endpoint.

    Reads the JPEG from disk and sends it as the multipart ``image`` field with
    ``trip`` / ``source`` / ``tags`` query params. Returns the raw response so
    the caller can branch on the status code (201 success, 422 no-GPS skip,
    anything else retryable).
    """
    source_path = Path(record.source_path)
    data = source_path.read_bytes()

    params = {
        "trip": trip,
        "source": source,
        "tags": ",".join(record.tags or []),
    }
    files = {"image": (source_path.name, data, "image/jpeg")}

    return await client.post(
        f"{base_url}{INGEST_PATH}",
        params=params,
        files=files,
        headers=headers,
    )


async def _run_upload(
    source_dir: Path,
    db_path: Path,
    trip: str,
    dry_run: bool,
    interval_seconds: int = 0,
    source: str = "gopro",
    tags: list[str] | None = None,
) -> None:
    """Main upload logic: scan, queue, then POST pending images to the endpoint."""
    queue = UploadQueue(db_path)

    # Reset interrupted uploads
    reset_count = queue.reset_uploading()
    if reset_count:
        print(f"Reset {reset_count} interrupted uploads")

    # Scan for new images
    print(f"Scanning {source_dir}...")
    images = scan_images(source_dir)
    print(f"Found {len(images)} images")

    # Note: even with no new files on disk there may be pending records from a
    # previous run, so do not early-return here.

    # Sample images by time interval (e.g., 60s = at least 1 image per minute)
    if images:
        print(
            "Extracting EXIF and sampling by time..."
            if interval_seconds > 0
            else "Extracting EXIF..."
        )
        sampled = sample_images_by_time(images, interval_seconds)
        if interval_seconds > 0:
            print(
                f"Sampled to {len(sampled)} images (at least 1 per {interval_seconds}s)"
            )
    else:
        sampled = []

    # Queue new images (EXIF already extracted during sampling)
    new_count = 0
    for img_path, lat, lng, timestamp, optics in sampled:
        # Generate deterministic key based on source + timestamp + filename
        dest_key = generate_dest_key(img_path, source, timestamp)

        record_id = queue.add(img_path, dest_key, lat, lng, timestamp, tags, optics)
        if record_id:
            new_count += 1
            gps_info = f"({lat:.4f}, {lng:.4f})" if lat and lng else "(no GPS)"
            tags_info = f" [{', '.join(tags)}]" if tags else ""
            optics_info = (
                f" [EV:{optics.light_value}]" if optics and optics.light_value else ""
            )
            print(
                f"  Queued: {img_path.name} -> {dest_key} {gps_info}{tags_info}{optics_info}"
            )

    if new_count:
        print(f"Queued {new_count} new images")
    else:
        print("No new images to queue")

    # Show queue status
    stats = queue.get_stats()
    pending_count = stats.get(UploadStatus.PENDING.value, 0)
    completed = stats.get(UploadStatus.COMPLETED.value, 0)
    failed = stats.get(UploadStatus.FAILED.value, 0)
    print(f"Queue: {pending_count} pending, {completed} completed, {failed} failed")

    if dry_run:
        print(f"\n[DRY RUN] Would POST pending images to {INGEST_PATH}")
        return

    # Get pending records (includes failed with retry_count < MAX_RETRIES)
    pending_records = queue.get_pending()
    if not pending_records:
        print("No pending uploads")
        return

    base_url, headers = ingest_config()

    # Process uploads with progress bar
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        with GracefulShutdown() as shutdown:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
            ) as progress:
                task = progress.add_task("Uploading...", total=len(pending_records))

                for record in pending_records:
                    if shutdown.shutdown_requested:
                        break

                    queue.mark_uploading(record.id)
                    source_file = Path(record.source_path)
                    progress.update(task, description=f"[cyan]{source_file.name}")

                    try:
                        resp = await post_image(
                            client, base_url, headers, record, trip, source
                        )
                    except httpx.HTTPError as e:
                        # Network/transport error: leave in queue to retry.
                        error_msg = str(e)
                        logger.warning("Upload error for %s: %s", record.dest_key, e)
                        queue.mark_failed(record.id, error_msg)
                        retry_info = (
                            f"retry {record.retry_count + 1}/{queue.MAX_RETRIES}"
                        )
                        progress.console.print(
                            f"[red][FAIL] {source_file.name}: {error_msg} ({retry_info})"
                        )
                        continue

                    if resp.status_code == 201:
                        queue.mark_completed(record.id)
                        progress.advance(task)
                    elif resp.status_code == 422:
                        # Server rejected: image has no GPS. Will never succeed,
                        # so dequeue permanently rather than retry forever.
                        queue.mark_skipped(record.id, "no GPS coordinates (HTTP 422)")
                        progress.advance(task)
                        progress.console.print(
                            f"[yellow][SKIP] {source_file.name}: no GPS coordinates"
                        )
                    else:
                        # Any other non-2xx is retryable (auth, 5xx, etc.).
                        error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
                        logger.warning(
                            "Upload failed for %s: %s", record.dest_key, error_msg
                        )
                        queue.mark_failed(record.id, error_msg)
                        retry_info = (
                            f"retry {record.retry_count + 1}/{queue.MAX_RETRIES}"
                        )
                        progress.console.print(
                            f"[red][FAIL] {source_file.name}: {error_msg} ({retry_info})"
                        )

    # Final stats
    final_stats = queue.get_stats()
    print(
        f"\nFinal: {final_stats.get(UploadStatus.COMPLETED.value, 0)} completed, "
        f"{final_stats.get(UploadStatus.SKIPPED.value, 0)} skipped, "
        f"{final_stats.get(UploadStatus.FAILED.value, 0)} failed"
    )


@app.command()
def scan(
    source_dir: Annotated[
        Path,
        typer.Argument(
            help="Directory to scan for images (e.g., /Volumes/SD_CARD/DCIM)"
        ),
    ],
    trip: Annotated[
        str,
        typer.Option("--trip", help="Trip slug to ingest into (e.g. 'vancouver-2025')"),
    ],
    db_path: Annotated[
        Path, typer.Option("--db", help="Path to upload queue database")
    ] = DB_PATH,
    interval: Annotated[
        int,
        typer.Option(
            "--interval",
            "-i",
            help="Minimum seconds between images (e.g., 60 for at least 1/min)",
        ),
    ] = 0,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", "-n", help="Scan and queue only, don't upload")
    ] = False,
    source: Annotated[
        str,
        typer.Option("--source", "-s", help="Image source (gopro, camera, phone)"),
    ] = "gopro",
    tags: Annotated[
        str,
        typer.Option(
            "--tags", "-t", help="Comma-separated tags (e.g., 'hotspring,wildlife')"
        ),
    ] = "",
) -> None:
    """
    Scan a directory for images and POST them to the trip ingestion endpoint.

    Recursively scans all subdirectories. Images are sorted by EXIF timestamp.
    Use --interval to sample at most one image per N seconds.

    Requires TRIPS_INGEST_URL in the environment (and, for the remote
    Cloudflare Access path, CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET).

    Example:
        # Upload all images for a trip
        publish-trip-images scan /Volumes/Untitled/DCIM/vancouver --trip vancouver-2025

        # Sample to at least 1 image per 60 seconds
        publish-trip-images scan /Volumes/Untitled/DCIM/vancouver --trip vancouver-2025 --interval 60

        # Tag images for filtering (e.g., hotspring, wildlife, food)
        publish-trip-images scan /path/to/trip --trip vancouver-2025 --tags hotspring,wildlife

        # Preview what would be selected (dry run)
        publish-trip-images scan /path/to/trip --trip vancouver-2025 --interval 60 --dry-run
    """
    if not source_dir.exists():
        print(f"Error: Directory not found: {source_dir}")
        raise typer.Exit(1)

    # Parse comma-separated tags into list
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    asyncio.run(
        _run_upload(source_dir, db_path, trip, dry_run, interval, source, tag_list)
    )


@app.command()
def status(
    db_path: Annotated[
        Path, typer.Option("--db", help="Path to upload queue database")
    ] = DB_PATH,
) -> None:
    """Show upload queue status."""
    if not db_path.exists():
        print("No upload history found")
        return

    queue = UploadQueue(db_path)
    stats = queue.get_stats()

    total = sum(stats.values())
    print(f"Total images: {total}")
    print(f"  Completed:  {stats.get(UploadStatus.COMPLETED.value, 0)}")
    print(f"  Pending:    {stats.get(UploadStatus.PENDING.value, 0)}")
    print(f"  Uploading:  {stats.get(UploadStatus.UPLOADING.value, 0)}")
    print(f"  Skipped:    {stats.get(UploadStatus.SKIPPED.value, 0)}")
    print(f"  Failed:     {stats.get(UploadStatus.FAILED.value, 0)}")

    # Show failed records
    pending = queue.get_pending()
    failed = [r for r in pending if r.status == UploadStatus.FAILED]
    if failed:
        print("\nFailed uploads:")
        for r in failed:
            print(f"  #{r.id} {Path(r.source_path).name}: {r.error_message}")


@app.command()
def retry(
    trip: Annotated[
        str,
        typer.Option("--trip", help="Trip slug to ingest the pending images into"),
    ],
    db_path: Annotated[
        Path, typer.Option("--db", help="Path to upload queue database")
    ] = DB_PATH,
    source: Annotated[
        str,
        typer.Option("--source", "-s", help="Image source (gopro, camera, phone)"),
    ] = "gopro",
) -> None:
    """Retry pending/failed uploads (no directory scan)."""
    if not db_path.exists():
        print("No upload history found")
        return

    queue = UploadQueue(db_path)
    pending = queue.get_pending()

    if not pending:
        print("No pending uploads to retry")
        return

    print(f"Retrying {len(pending)} uploads...")
    # Use a dummy source dir since we're only retrying existing records
    asyncio.run(_run_upload(Path("."), db_path, trip, dry_run=False, source=source))


@app.command()
def fix_timestamps(
    db_path: Annotated[
        Path, typer.Option("--db", help="Path to upload queue database")
    ] = DB_PATH,
) -> None:
    """Fix timestamps by removing incorrect 'Z' suffix (EXIF times are local, not UTC)."""
    if not db_path.exists():
        print("No upload history found")
        return

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE images SET timestamp = REPLACE(timestamp, 'Z', '') WHERE timestamp LIKE '%Z'"
        )
        conn.commit()
        print(f"Fixed {cursor.rowcount} timestamps")


if __name__ == "__main__":
    app()
