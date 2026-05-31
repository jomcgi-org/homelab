"""
Publish Trip Images

Scans a directory (e.g., SD card) for images, extracts EXIF metadata,
uploads to SeaweedFS, and publishes trip points to NATS.
"""

import asyncio
import hashlib
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

from botocore.exceptions import ClientError

import boto3
import nats
from botocore.config import Config
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

from projects.trips.tools.elevation import ElevationClient

# Defaults
DB_PATH = Path(__file__).parent / "publish_queue.db"
DEFAULT_BUCKET = "trips"

# SeaweedFS S3 endpoint (for local dev, use port-forward or external URL)
SEAWEEDFS_ENDPOINT = os.getenv("SEAWEEDFS_ENDPOINT", "http://localhost:8333")
NATS_URL = os.getenv("NATS_URL", "nats://localhost:4222")

# Namespace UUID for deterministic image key generation
# This ensures the same source+timestamp always produces the same key
IMAGE_KEY_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

logger = logging.getLogger(__name__)

app = typer.Typer(help="Publish trip images to SeaweedFS and NATS")


class UploadStatus(str, Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"


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
            # Migrations: add columns if they don't exist
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
    """Extract GPS coordinates, timestamp, and OPTICS data from EXIF."""
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


def get_s3_client():
    """Create S3 client for SeaweedFS."""
    return boto3.client(
        "s3",
        endpoint_url=SEAWEEDFS_ENDPOINT,
        aws_access_key_id="any",  # SeaweedFS with auth disabled
        aws_secret_access_key="any",
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def ensure_bucket(s3_client, bucket: str) -> None:
    """Create bucket if it doesn't exist."""
    try:
        s3_client.head_bucket(Bucket=bucket)
    except s3_client.exceptions.ClientError:
        print(f"Creating bucket: {bucket}")
        s3_client.create_bucket(Bucket=bucket)


class OpticsCache:
    """SQLite cache for OPTICS data to avoid re-downloading images."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS optics_cache (
                    image_key TEXT PRIMARY KEY,
                    light_value REAL,
                    iso INTEGER,
                    shutter_speed TEXT,
                    aperture REAL,
                    focal_length_35mm INTEGER,
                    cached_at TEXT NOT NULL
                )
            """)
            conn.commit()

    def get(self, image_key: str) -> tuple[bool, OpticsData | None]:
        """Returns (found_in_cache, optics_data)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM optics_cache WHERE image_key = ?", (image_key,)
            ).fetchone()
            if not row:
                return False, None
            return True, OpticsData(
                light_value=row["light_value"],
                iso=row["iso"],
                shutter_speed=row["shutter_speed"],
                aperture=row["aperture"],
                focal_length_35mm=row["focal_length_35mm"],
            )

    def put(self, image_key: str, optics: OpticsData | None) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO optics_cache
                (image_key, light_value, iso, shutter_speed, aperture, focal_length_35mm, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_key,
                    optics.light_value if optics else None,
                    optics.iso if optics else None,
                    optics.shutter_speed if optics else None,
                    optics.aperture if optics else None,
                    optics.focal_length_35mm if optics else None,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()

    def stats(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM optics_cache").fetchone()[0]


OPTICS_CACHE_PATH = Path(__file__).parent / "optics_cache.db"


def list_s3_keys(s3_client, bucket: str, prefix: str = "") -> list[str]:
    """List all object keys in an S3 bucket (paginated).

    Only returns keys with image extensions (.jpg, .jpeg, .png, .heic, .heif).
    """
    image_extensions = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
    keys = []
    continuation_token = None

    while True:
        kwargs = {"Bucket": bucket, "MaxKeys": 1000}
        if prefix:
            kwargs["Prefix"] = prefix
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        response = s3_client.list_objects_v2(**kwargs)

        for obj in response.get("Contents", []):
            key = obj["Key"]
            ext = Path(key).suffix.lower()
            if ext in image_extensions:
                keys.append(key)

        if not response.get("IsTruncated"):
            break
        continuation_token = response["NextContinuationToken"]

    return keys


def _rebuild_batch(
    s3_client,
    bucket: str,
    keys: list[str],
    queue: UploadQueue,
    optics_cache: OpticsCache,
    tmp_dir: Path,
    concurrency: int,
) -> list[dict]:
    """Download a batch of images from S3, extract EXIF, add to queue.

    Returns list of point dicts (with lat/lng/timestamp/optics) for later
    elevation lookup and NATS publishing. Cleans up downloaded files after.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    points = []

    def download_and_extract(
        key: str,
    ) -> tuple[str, float | None, float | None, str | None, OpticsData | None]:
        """Download one image and extract EXIF. Runs in thread pool."""
        # Check optics cache first
        found, cached_optics = optics_cache.get(key)

        local_path = tmp_dir / key
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            s3_client.download_file(bucket, key, str(local_path))
            lat, lng, timestamp, optics = extract_exif(local_path)

            # Cache optics result
            if not found:
                optics_cache.put(key, optics)
            elif cached_optics:
                # Use cached optics if we had them (extract_exif may return same)
                optics = cached_optics

            return key, lat, lng, timestamp, optics
        except Exception as e:
            logger.warning("Failed to process %s: %s", key, e)
            print(f"  Warning: Failed to process {key}: {e}")
            return key, None, None, None, None

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(download_and_extract, key): key for key in keys}

        for future in as_completed(futures):
            key, lat, lng, timestamp, optics = future.result()

            # Skip images without GPS
            if lat is None or lng is None:
                continue

            # Add to queue DB (source_path is S3 key since local path is gone)
            record_id = queue.add(
                source_path=Path(key),
                dest_key=key,
                lat=lat,
                lng=lng,
                timestamp=timestamp,
                tags=None,
                optics=optics,
            )

            # Mark as completed immediately (image already in S3)
            if record_id:
                queue.mark_completed(record_id)

            # Collect point for elevation + NATS publishing
            point = {
                "key": key,
                "lat": lat,
                "lng": lng,
                "timestamp": timestamp,
                "optics": optics,
            }
            points.append(point)

    # Clean up downloaded files
    import shutil

    shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    return points


def calculate_md5(file_path: Path) -> str:
    """Calculate MD5 hash of a file for S3 ETag comparison."""
    md5 = hashlib.md5(usedforsecurity=False)  # nosec: MD5 required for S3 ETag match
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    return md5.hexdigest()


def object_exists_with_hash(s3_client, bucket: str, key: str, local_hash: str) -> bool:
    """Check if object exists in S3 with matching hash (ETag)."""
    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
        # ETag is quoted, e.g., '"d41d8cd98f00b204e9800998ecf8427e"'
        etag = response.get("ETag", "").strip('"')
        return etag == local_hash
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        # For other errors, assume we need to upload
        return False


def upload_image(s3_client, bucket: str, source_path: Path, dest_key: str) -> bool:
    """Upload image to SeaweedFS. Returns True if uploaded, False if skipped (already exists)."""
    # Check if file already exists with same hash
    local_hash = calculate_md5(source_path)
    if object_exists_with_hash(s3_client, bucket, dest_key, local_hash):
        return False  # Skip upload, file unchanged

    content_type = "image/jpeg"
    if source_path.suffix.lower() == ".png":
        content_type = "image/png"
    elif source_path.suffix.lower() in (".heic", ".heif"):
        content_type = "image/heic"

    s3_client.upload_file(
        str(source_path),
        bucket,
        dest_key,
        ExtraArgs={"ContentType": content_type},
    )
    return True  # Uploaded


async def get_jetstream() -> tuple:
    """Connect to NATS and return (connection, jetstream) tuple."""
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()

    # Ensure stream exists
    try:
        await js.stream_info("trips")
    except nats.js.errors.NotFoundError:
        await js.add_stream(name="trips", subjects=["trips.>"])

    return nc, js


async def publish_to_nats(js, record: ImageRecord, source: str) -> None:
    """Publish trip point to NATS JetStream."""
    # Extract deterministic ID from dest_key (e.g., "img_abc123def456.jpg" -> "abc123def456")
    # This ensures the same image always gets the same ID
    key_hex = record.dest_key.replace("img_", "").rsplit(".", 1)[0]

    # Build trip point message
    # 5 decimal places = ~1m precision (sufficient for 5-10m requirement)
    point = {
        "id": key_hex,  # Deterministic string ID from UUID
        "lat": round(record.lat, 5) if record.lat else 0.0,
        "lng": round(record.lng, 5) if record.lng else 0.0,
        "timestamp": record.timestamp or datetime.now().isoformat(),
        "image": record.dest_key,  # Frontend constructs full/thumb URLs
        "source": source,
        "tags": record.tags or [],  # User-defined tags
    }

    # Include OPTICS data if available
    if record.optics:
        if record.optics.light_value is not None:
            point["light_value"] = record.optics.light_value
        if record.optics.iso is not None:
            point["iso"] = record.optics.iso
        if record.optics.shutter_speed is not None:
            point["shutter_speed"] = record.optics.shutter_speed
        if record.optics.aperture is not None:
            point["aperture"] = record.optics.aperture
        if record.optics.focal_length_35mm is not None:
            point["focal_length_35mm"] = record.optics.focal_length_35mm

    await js.publish("trips.point", json.dumps(point).encode())


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
    """Generate deterministic destination key for S3.

    Uses UUID5 (deterministic) based on:
    - source (gopro, camera, phone)
    - EXIF timestamp (if available)
    - original filename (as fallback/disambiguation)

    This ensures the same image always gets the same key, even if:
    - The image is rotated/edited (timestamp preserved)
    - The script is re-run from scratch
    - The database is deleted
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


async def _run_upload(
    source_dir: Path,
    db_path: Path,
    bucket: str,
    dry_run: bool,
    publish: bool,
    interval_seconds: int = 0,
    source: str = "gopro",
    tags: list[str] | None = None,
) -> None:
    """Main upload logic."""
    queue = UploadQueue(db_path)

    # Reset interrupted uploads
    reset_count = queue.reset_uploading()
    if reset_count:
        print(f"Reset {reset_count} interrupted uploads")

    # Scan for new images
    print(f"Scanning {source_dir}...")
    images = scan_images(source_dir)
    print(f"Found {len(images)} images")

    if not images:
        return

    # Sample images by time interval (e.g., 60s = at least 1 image per minute)
    print(
        "Extracting EXIF and sampling by time..."
        if interval_seconds > 0
        else "Extracting EXIF..."
    )
    sampled = sample_images_by_time(images, interval_seconds)
    if interval_seconds > 0:
        print(f"Sampled to {len(sampled)} images (at least 1 per {interval_seconds}s)")

    if not sampled:
        return

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
        print("\n[DRY RUN] Would upload to SeaweedFS and publish to NATS")
        return

    # Get pending records (includes failed with retry_count < MAX_RETRIES)
    pending_records = queue.get_pending()
    if not pending_records:
        print("No pending uploads")
        return

    # Create S3 client and ensure bucket
    s3_client = get_s3_client()
    ensure_bucket(s3_client, bucket)

    # Connect to NATS once if publishing
    nc, js = None, None
    if publish:
        nc, js = await get_jetstream()

    # Process uploads with progress bar
    try:
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
                        # Upload to S3 (skips if file unchanged)
                        uploaded = upload_image(
                            s3_client, bucket, source_file, record.dest_key
                        )

                        # Publish to NATS if requested (even if upload skipped - metadata may differ)
                        if publish and js:
                            await publish_to_nats(js, record, source)

                        queue.mark_completed(record.id)
                        progress.advance(task)

                    except Exception as e:
                        error_msg = str(e)
                        logger.exception("Upload failed for %s", record.dest_key)
                        queue.mark_failed(record.id, error_msg)
                        retry_info = (
                            f"retry {record.retry_count + 1}/{queue.MAX_RETRIES}"
                        )
                        progress.console.print(
                            f"[red][FAIL] {source_file.name}: {error_msg} ({retry_info})"
                        )
    finally:
        if nc:
            await nc.close()

    # Final stats
    final_stats = queue.get_stats()
    print(
        f"\nFinal: {final_stats.get(UploadStatus.COMPLETED.value, 0)} completed, "
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
    db_path: Annotated[
        Path, typer.Option("--db", help="Path to upload queue database")
    ] = DB_PATH,
    bucket: Annotated[
        str, typer.Option("--bucket", "-b", help="S3 bucket name")
    ] = DEFAULT_BUCKET,
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
    publish: Annotated[
        bool, typer.Option("--publish", "-p", help="Publish to NATS after upload")
    ] = True,
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
    Scan a directory for images and upload to SeaweedFS.

    Recursively scans all subdirectories. Images are sorted by EXIF timestamp.
    Use --interval to sample at most one image per N seconds.

    Example:
        # Upload all images
        publish-trip-images scan /Volumes/Untitled/DCIM/vancouver-to-kamloops

        # Sample to at least 1 image per 60 seconds
        publish-trip-images scan /Volumes/Untitled/DCIM/vancouver-to-kamloops --interval 60

        # Tag images for filtering (e.g., hotspring, wildlife, food)
        publish-trip-images scan /path/to/trip --tags hotspring,wildlife

        # Preview what would be selected (dry run)
        publish-trip-images scan /path/to/trip --interval 60 --dry-run
    """
    if not source_dir.exists():
        print(f"Error: Directory not found: {source_dir}")
        raise typer.Exit(1)

    # Parse comma-separated tags into list
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    asyncio.run(
        _run_upload(
            source_dir, db_path, bucket, dry_run, publish, interval, source, tag_list
        )
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
    db_path: Annotated[
        Path, typer.Option("--db", help="Path to upload queue database")
    ] = DB_PATH,
    bucket: Annotated[
        str, typer.Option("--bucket", "-b", help="S3 bucket name")
    ] = DEFAULT_BUCKET,
    publish: Annotated[
        bool, typer.Option("--publish", "-p", help="Publish to NATS after upload")
    ] = True,
) -> None:
    """Retry failed uploads."""
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
    asyncio.run(_run_upload(Path("."), db_path, bucket, dry_run=False, publish=publish))


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


@app.command()
def publish_all(
    db_path: Annotated[
        Path, typer.Option("--db", help="Path to upload queue database")
    ] = DB_PATH,
    source: Annotated[
        str,
        typer.Option("--source", "-s", help="Image source (gopro, camera, phone)"),
    ] = "gopro",
) -> None:
    """Publish all completed uploads to NATS (useful for re-sync)."""
    if not db_path.exists():
        print("No upload history found")
        return

    queue = UploadQueue(db_path)
    completed = queue.get_completed()

    if not completed:
        print("No completed uploads to publish")
        return

    async def _publish_all():
        print(f"Publishing {len(completed)} points to NATS (source={source})...")
        nc, js = await get_jetstream()
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
            ) as progress:
                task = progress.add_task("Publishing...", total=len(completed))

                for record in completed:
                    progress.update(task, description=f"[cyan]{record.dest_key}")
                    try:
                        await publish_to_nats(js, record, source)
                        progress.advance(task)
                    except Exception as e:
                        logger.warning(
                            "NATS publish failed for %s: %s", record.dest_key, e
                        )
                        progress.console.print(f"[red][FAIL] {record.dest_key}: {e}")
        finally:
            await nc.close()

    asyncio.run(_publish_all())
    print("Done")


@app.command()
def backfill_optics(
    bucket: Annotated[
        str, typer.Option("--bucket", "-b", help="S3 bucket name")
    ] = DEFAULT_BUCKET,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", "-n", help="Show what would be published without publishing"
        ),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit", "-l", help="Limit number of points to process (0 = all)"
        ),
    ] = 0,
    concurrency: Annotated[
        int,
        typer.Option("--concurrency", "-c", help="Number of parallel downloads"),
    ] = 10,
) -> None:
    """Backfill OPTICS data from SeaweedFS raw images to NATS.

    Replays all points from NATS, downloads each image from SeaweedFS,
    extracts OPTICS EXIF data (ISO, aperture, shutter speed, focal length,
    light value), and republishes to NATS with the enriched metadata.

    Preserves existing point data (tags, source, etc.) - only adds OPTICS fields.
    Uses SQLite cache to avoid re-downloading images for duplicate points.

    Example:
        # Preview what would be backfilled
        backfill-optics --dry-run

        # Backfill all points
        backfill-optics

        # Backfill with 20 parallel downloads
        backfill-optics --concurrency 20

        # Backfill first 10 points (for testing)
        backfill-optics --limit 10
    """
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    cache = OpticsCache(OPTICS_CACHE_PATH)
    print(f"OPTICS cache: {cache.stats()} entries")

    s3_client = get_s3_client()

    def download_and_extract(image_key: str) -> tuple[bool, OpticsData | None]:
        """Download image and extract OPTICS (runs in thread pool).
        Returns (from_cache, optics_data).
        """
        # Check cache first
        found, cached = cache.get(image_key)
        if found:
            return True, cached

        # Download and extract
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=True) as tmp:
            try:
                s3_client.download_file(bucket, image_key, tmp.name)
                _, _, _, optics = extract_exif(Path(tmp.name))
                # Cache result (even if None)
                cache.put(image_key, optics)
                return False, optics
            except Exception:
                logger.warning("failed to download or extract EXIF for %s", image_key)
                return False, None

    async def _backfill():
        nc = await nats.connect(NATS_URL)
        js = nc.jetstream()

        # Replay all existing points from NATS stream
        print("Replaying points from NATS stream...")
        points = []
        try:
            consumer = await js.pull_subscribe(
                "trips.>",
                stream="trips",
                config=nats.js.api.ConsumerConfig(
                    deliver_policy=nats.js.api.DeliverPolicy.ALL,
                    ack_policy=nats.js.api.AckPolicy.NONE,
                ),
            )

            while True:
                try:
                    msgs = await consumer.fetch(batch=100, timeout=1)
                    for msg in msgs:
                        try:
                            point_data = json.loads(msg.data.decode())
                            # Skip tombstone/delete messages
                            if not point_data.get("deleted"):
                                points.append(point_data)
                        except Exception:
                            logger.warning("failed to decode NATS message, skipping")
                            pass
                except nats.errors.TimeoutError:
                    break

            await consumer.unsubscribe()
        except nats.js.errors.StreamNotFoundError:
            print("Stream 'trips' not found")
            await nc.close()
            return 0, 0, 0, 0

        print(f"Found {len(points)} points in NATS stream")

        # Filter to points with images that don't already have OPTICS
        points_to_process = [
            p
            for p in points
            if p.get("image") and not (p.get("iso") or p.get("light_value"))
        ]
        skipped_already_has = len(
            [p for p in points if p.get("iso") or p.get("light_value")]
        )

        print(f"  {len(points_to_process)} need OPTICS data")
        if skipped_already_has:
            print(f"  {skipped_already_has} already have OPTICS data")

        if limit > 0:
            points_to_process = points_to_process[:limit]
            print(f"Processing first {limit} points")

        if not points_to_process:
            print("No points to process")
            await nc.close()
            return 0, 0, skipped_already_has, 0

        # Get unique images to download
        unique_images = list({p["image"] for p in points_to_process})
        print(f"  {len(unique_images)} unique images to process")

        # Download and extract OPTICS in parallel using thread pool
        optics_by_image: dict[str, OpticsData | None] = {}
        cache_hits = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
        ) as progress:
            # Phase 1: Download and extract (parallelized)
            task = progress.add_task(
                f"Extracting OPTICS ({concurrency} workers)...",
                total=len(unique_images),
            )

            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                # Submit all images - download_and_extract checks cache internally
                futures = {
                    executor.submit(download_and_extract, img): img
                    for img in unique_images
                }

                from concurrent.futures import as_completed

                for future in as_completed(futures):
                    image_key = futures[future]
                    try:
                        from_cache, optics = future.result()
                        optics_by_image[image_key] = optics
                        if from_cache:
                            cache_hits += 1
                    except Exception as e:
                        logger.warning(
                            "Failed to extract EXIF for %s: %s", image_key, e
                        )
                        progress.console.print(f"[red][SKIP] {image_key}: {e}")
                    progress.advance(task)

            if cache_hits:
                progress.console.print(
                    f"[green]Cache hits: {cache_hits}/{len(unique_images)}"
                )

            # Phase 2: Publish enriched points
            task2 = progress.add_task(
                "Publishing to NATS...", total=len(points_to_process)
            )
            processed = 0
            with_optics = 0

            for point in points_to_process:
                image_key = point["image"]
                optics = optics_by_image.get(image_key)

                # Build enriched point - preserve all existing data
                enriched_point = dict(point)

                # Add OPTICS data if available
                if optics:
                    with_optics += 1
                    if optics.light_value is not None:
                        enriched_point["light_value"] = optics.light_value
                    if optics.iso is not None:
                        enriched_point["iso"] = optics.iso
                    if optics.shutter_speed is not None:
                        enriched_point["shutter_speed"] = optics.shutter_speed
                    if optics.aperture is not None:
                        enriched_point["aperture"] = optics.aperture
                    if optics.focal_length_35mm is not None:
                        enriched_point["focal_length_35mm"] = optics.focal_length_35mm

                if dry_run:
                    optics_str = ""
                    if optics:
                        parts = []
                        if optics.light_value:
                            parts.append(f"EV:{optics.light_value}")
                        if optics.iso:
                            parts.append(f"ISO:{optics.iso}")
                        if optics.shutter_speed:
                            parts.append(optics.shutter_speed)
                        if optics.aperture:
                            parts.append(f"ƒ/{optics.aperture}")
                        if optics.focal_length_35mm:
                            parts.append(f"{optics.focal_length_35mm}mm")
                        optics_str = " [" + " ".join(parts) + "]" if parts else ""
                    progress.console.print(f"[dim]{image_key}{optics_str}")
                else:
                    await js.publish("trips.point", json.dumps(enriched_point).encode())

                processed += 1
                progress.advance(task2)

        await nc.close()
        return processed, with_optics, skipped_already_has, cache_hits

    result = asyncio.run(_backfill())
    processed, with_optics, skipped, cache_hits = result

    print(f"\nOPTICS cache now has {cache.stats()} entries")

    if dry_run:
        print(
            f"[DRY RUN] Would publish {processed} points ({with_optics} with OPTICS data)"
        )
    else:
        print(f"Published {processed} points ({with_optics} with OPTICS data)")
    if skipped:
        print(f"  Skipped {skipped} points that already have OPTICS data")


async def _run_rebuild(
    bucket: str,
    batch_size: int,
    concurrency: int,
    dry_run: bool,
    source: str,
    db_path: Path,
    fix_retention: bool,
) -> None:
    """Main rebuild logic."""
    import shutil
    import tempfile

    queue = UploadQueue(db_path)
    optics_cache = OpticsCache(OPTICS_CACHE_PATH)
    s3_client = get_s3_client()

    # Step 1: Connect to NATS
    print("Connecting to NATS...")
    nc, js = await get_jetstream()

    try:
        # Step 2: Fix stream retention
        if fix_retention and not dry_run:
            try:
                stream_info = await js.stream_info("trips")
                config = stream_info.config
                if config.max_age and config.max_age > 0:
                    config.max_age = 0  # unlimited
                    await js.update_stream(config)
                    print("Fixed NATS stream max_age: removed 30d TTL (now unlimited)")
                else:
                    print("NATS stream max_age already unlimited")
            except Exception as e:
                logger.warning("Could not update stream retention: %s", e)
                print(f"Warning: Could not update stream retention: {e}")

        # Step 3: List all S3 keys
        print(f"Listing objects in s3://{bucket}...")
        all_keys = list_s3_keys(s3_client, bucket)
        print(f"Found {len(all_keys)} images in S3")

        if not all_keys:
            return

        # Step 4: Process in batches
        tmp_dir = Path(tempfile.mkdtemp(prefix="rebuild-"))
        all_points = []

        try:
            num_batches = (len(all_keys) + batch_size - 1) // batch_size

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
            ) as progress:
                task = progress.add_task(
                    f"Processing batches (0/{num_batches})...",
                    total=len(all_keys),
                )

                for batch_idx in range(0, len(all_keys), batch_size):
                    batch_keys = all_keys[batch_idx : batch_idx + batch_size]
                    batch_num = batch_idx // batch_size + 1

                    progress.update(
                        task,
                        description=f"Batch {batch_num}/{num_batches} ({len(batch_keys)} images)...",
                    )

                    batch_points = _rebuild_batch(
                        s3_client=s3_client,
                        bucket=bucket,
                        keys=batch_keys,
                        queue=queue,
                        optics_cache=optics_cache,
                        tmp_dir=tmp_dir,
                        concurrency=concurrency,
                    )

                    all_points.extend(batch_points)
                    progress.advance(task, advance=len(batch_keys))

        finally:
            # Clean up temp dir
            shutil.rmtree(tmp_dir, ignore_errors=True)

        no_gps = len(all_keys) - len(all_points)
        print(
            f"\nExtracted {len(all_points)} points with GPS ({no_gps} skipped, no GPS)"
        )
        print(f"Optics cache: {optics_cache.stats()} entries")

        if not all_points:
            print("No points to publish")
            return

        # Step 5: Fetch elevation
        coords = [(p["lat"], p["lng"]) for p in all_points]
        print(f"\nFetching elevation for {len(coords)} points...")

        async with ElevationClient() as elev_client:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
            ) as progress:
                task = progress.add_task("Fetching elevations...", total=len(coords))

                def update_progress(completed, total):
                    progress.update(task, completed=completed)

                results = await elev_client.get_elevations(
                    coords, progress_callback=update_progress
                )

        with_elevation = sum(1 for r in results if r.elevation is not None)
        cached = sum(1 for r in results if r.cached)
        print(
            f"Elevation: {with_elevation}/{len(results)} resolved ({cached} from cache)"
        )

        # Attach elevation to points
        for point, result in zip(all_points, results):
            point["elevation"] = result.elevation

        if dry_run:
            print(f"\n[DRY RUN] Would publish {len(all_points)} points to NATS")
            # Show sample
            for p in all_points[:5]:
                elev = f"{p['elevation']:.0f}m" if p["elevation"] else "none"
                optics_str = ""
                if p["optics"] and p["optics"].light_value:
                    optics_str = f" EV:{p['optics'].light_value}"
                print(
                    f"  {p['key']} ({p['lat']:.4f}, {p['lng']:.4f}) elev={elev}{optics_str}"
                )
            if len(all_points) > 5:
                print(f"  ... and {len(all_points) - 5} more")
            return

        # Step 6: Publish to NATS
        print(f"\nPublishing {len(all_points)} points to NATS...")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
        ) as progress:
            task = progress.add_task("Publishing...", total=len(all_points))

            for point in all_points:
                key = point["key"]
                key_hex = key.replace("img_", "").rsplit(".", 1)[0]

                msg = {
                    "id": key_hex,
                    "lat": round(point["lat"], 5),
                    "lng": round(point["lng"], 5),
                    "timestamp": point["timestamp"] or datetime.now().isoformat(),
                    "image": key,
                    "source": source,
                    "tags": [],
                }

                if point["elevation"] is not None:
                    msg["elevation"] = point["elevation"]

                optics = point["optics"]
                if optics:
                    if optics.light_value is not None:
                        msg["light_value"] = optics.light_value
                    if optics.iso is not None:
                        msg["iso"] = optics.iso
                    if optics.shutter_speed is not None:
                        msg["shutter_speed"] = optics.shutter_speed
                    if optics.aperture is not None:
                        msg["aperture"] = optics.aperture
                    if optics.focal_length_35mm is not None:
                        msg["focal_length_35mm"] = optics.focal_length_35mm

                await js.publish("trips.point", json.dumps(msg).encode())
                progress.advance(task)

        # Step 7: Final stats
        queue_stats = queue.get_stats()
        print(f"\nDone! Published {len(all_points)} points to NATS")
        print(f"  With elevation: {with_elevation}")
        print(
            f"  Queue DB: {queue_stats.get(UploadStatus.COMPLETED.value, 0)} completed"
        )

    finally:
        await nc.close()


@app.command()
def rebuild(
    bucket: Annotated[
        str, typer.Option("--bucket", "-b", help="S3 bucket name")
    ] = DEFAULT_BUCKET,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="Images per download batch")
    ] = 80,
    concurrency: Annotated[
        int, typer.Option("--concurrency", "-c", help="Parallel downloads per batch")
    ] = 10,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Extract and report only, don't publish"),
    ] = False,
    source: Annotated[
        str,
        typer.Option("--source", "-s", help="Image source tag"),
    ] = "gopro",
    db_path: Annotated[
        Path, typer.Option("--db", help="Path to upload queue database")
    ] = DB_PATH,
    fix_retention: Annotated[
        bool,
        typer.Option(
            "--fix-retention/--no-fix-retention",
            help="Fix NATS stream max_age to unlimited",
        ),
    ] = True,
) -> None:
    """Rebuild trip data from SeaweedFS when NATS stream data has been lost.

    Lists all images in the S3 bucket, downloads in batches to extract EXIF
    metadata (GPS, timestamps, camera settings), fetches elevation from NRCan,
    populates the local queue DB, and republishes all points to NATS.

    Disk usage is capped by processing images in batches (default 80 images
    ~440MB per batch). Temp files are cleaned between batches.

    Example:
        # Preview what would be recovered (dry run)
        publish-trip-images rebuild --dry-run

        # Full rebuild with elevation
        publish-trip-images rebuild

        # Custom batch size for lower disk usage
        publish-trip-images rebuild --batch-size 40
    """
    asyncio.run(
        _run_rebuild(
            bucket=bucket,
            batch_size=batch_size,
            concurrency=concurrency,
            dry_run=dry_run,
            source=source,
            db_path=db_path,
            fix_retention=fix_retention,
        )
    )


if __name__ == "__main__":
    app()
