#!/usr/bin/env python3
"""Generate bundled data file directly from database and weather API."""

import json
import logging
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import brotli
import requests  # nosemgrep: no-requests
from botocore.config import Config
from dateutil import parser as date_parser
from pydantic import BaseModel

# Add the original project to path to reuse some modules
# sys.path.insert(0, str(Path(__file__).parent.parent.parent / "cluster/services/find-good-hikes/app"))

# from config import *

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class Walk(BaseModel):
    """Walk data model."""

    uuid: str
    name: str
    url: str
    distance_km: float
    ascent_m: int
    duration_h: float
    summary: str
    latitude: float
    longitude: float


class S3Uploader:
    """Upload files to Cloudflare R2."""

    def __init__(self):
        self.s3_client = boto3.client(
            "s3",
            endpoint_url=os.environ["CLOUDFLARE_S3_ENDPOINT"],
            aws_access_key_id=os.environ["CLOUDFLARE_S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["CLOUDFLARE_S3_ACCESS_KEY_SECRET"],
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
            region_name="auto",
        )
        self.bucket_name = os.environ.get("R2_BUCKET_NAME", "jomcgi-hikes")


def load_walks_from_db() -> list[Walk]:
    """Load all walks from the SQLite database."""
    db_path = Path(__file__).parent.parent / "scrape_walkhighlands" / "walks.db"

    if not db_path.exists():
        logger.error(f"Database not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT uuid, name, url, distance_km, ascent_m, duration_h,
               summary, latitude, longitude
        FROM walks
        ORDER BY name
    """)

    walks = []
    for row in cursor.fetchall():
        walk = Walk(
            uuid=row["uuid"],
            name=row["name"],
            url=row["url"],
            distance_km=row["distance_km"],
            ascent_m=row["ascent_m"],
            duration_h=row["duration_h"],
            summary=row["summary"] or "",
            latitude=row["latitude"],
            longitude=row["longitude"],
        )
        walks.append(walk)

    conn.close()
    logger.info(f"Loaded {len(walks)} walks from database")
    return walks


def fetch_weather_forecast(lat: float, lon: float) -> dict[str, Any]:
    """Fetch weather forecast from met.no API."""
    url = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
    params = {"lat": round(lat, 4), "lon": round(lon, 4)}
    headers = {"User-Agent": "hikes.jomcgi.dev (https://github.com/jomcgi/homelab)"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch weather for ({lat}, {lon}): {e}")
        return None


def parse_weather_data(forecast_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse met.no forecast data into hourly windows."""
    if not forecast_data or "properties" not in forecast_data:
        return []

    timeseries = forecast_data["properties"]["timeseries"]
    hourly_data = []

    for entry in timeseries:
        time_str = entry["time"]
        data = entry["data"]
        instant = data.get("instant", {}).get("details", {})
        next_1_hours = data.get("next_1_hours", {})

        # Skip if no hourly data
        if not next_1_hours:
            continue

        hourly_data.append(
            {
                "time": time_str,
                "temp_c": instant.get("air_temperature"),
                "wind_speed_ms": instant.get("wind_speed"),
                "precipitation_mm": next_1_hours.get("details", {}).get(
                    "precipitation_amount", 0
                ),
                "cloud_area_fraction": instant.get("cloud_area_fraction"),
            }
        )

    return hourly_data


def is_weather_viable(weather: dict[str, Any]) -> bool:
    """Check if weather conditions are viable for hiking."""
    precip = weather.get("precipitation_mm", 0)
    wind_ms = weather.get("wind_speed_ms", 0)

    # Convert m/s to km/h
    wind_kmh = wind_ms * 3.6 if wind_ms is not None else 0

    # Apply viability thresholds
    if precip > 2.0:
        return False
    if wind_kmh > 80.0:
        return False

    return True


def is_daylight_hour(time_str: str, lat: float, lon: float) -> bool:
    """Check if the given time is during daylight hours."""
    dt = date_parser.parse(time_str)
    local_hour = dt.hour

    # Basic daylight check
    return 7 <= local_hour <= 19


def process_walk(walk: Walk) -> dict[str, Any]:
    """Process a single walk: fetch weather and return formatted data."""
    windows = []

    # Fetch weather forecast
    forecast_data = fetch_weather_forecast(walk.latitude, walk.longitude)
    if forecast_data:
        # Parse hourly data
        hourly_data = parse_weather_data(forecast_data)

        # Current time for filtering expired windows
        now = datetime.now(UTC)

        for weather in hourly_data:
            time_str = weather["time"]
            dt = date_parser.parse(time_str)

            # Skip past times
            if dt < now:
                continue

            # Skip if beyond forecast horizon
            if dt > now + timedelta(days=7):
                continue

            # Skip night hours
            if not is_daylight_hour(time_str, walk.latitude, walk.longitude):
                continue

            # Skip non-viable weather
            if not is_weather_viable(weather):
                continue

            # Add viable window
            # Format: [timestamp, temp, precip, wind, cloud]
            timestamp = int(dt.timestamp())
            temp_c = round(weather["temp_c"], 1) if weather["temp_c"] is not None else 0
            precip_mm = (
                round(weather["precipitation_mm"], 1)
                if weather["precipitation_mm"] > 0
                else 0
            )
            wind_ms = weather["wind_speed_ms"] or 0
            wind_kmh = round(wind_ms * 3.6)
            cloud_pct = (
                round(weather["cloud_area_fraction"])
                if weather["cloud_area_fraction"] is not None
                else 50
            )

            windows.append([timestamp, temp_c, precip_mm, wind_kmh, cloud_pct])

    return {"walk": walk, "windows": windows}


def create_bundle(walks_data: list[dict[str, Any]]) -> dict[str, Any]:
    """Create optimized bundle format."""
    bundle = {
        "v": 2,  # Version
        "g": int(time.time()),  # Generated timestamp
        "d": [],  # Data array
    }

    for data in walks_data:
        walk = data["walk"]
        windows = data["windows"]

        # Compact walk entry
        # Format: [id, lat, lng, dur, dist, asc, name, url, summary, windows]
        walk_entry = [
            walk.uuid,
            round(walk.latitude, 4),
            round(walk.longitude, 4),
            round(walk.duration_h, 1),
            round(walk.distance_km, 1),
            walk.ascent_m,
            walk.name,
            walk.url,
            walk.summary,
            windows,
        ]
        bundle["d"].append(walk_entry)

    return bundle


def main():
    """Generate and upload bundled data."""
    logger.info("Starting bundled data generation...")

    try:
        # Load walks from database
        walks = load_walks_from_db()

        # Process walks in parallel
        logger.info(f"Fetching weather data for {len(walks)} walks...")
        processed_walks = []

        # Use ThreadPoolExecutor for parallel weather fetching
        # Limit to 20 requests/second as per Met.no guidelines
        with ThreadPoolExecutor(max_workers=20) as executor:
            # Submit all walks for processing
            future_to_walk = {
                executor.submit(process_walk, walk): walk for walk in walks
            }

            # Process completed results
            for i, future in enumerate(as_completed(future_to_walk)):
                walk = future_to_walk[future]
                try:
                    result = future.result()
                    processed_walks.append(result)

                    if (i + 1) % 100 == 0:
                        logger.info(f"Processed {i + 1}/{len(walks)} walks")

                except Exception as e:
                    logger.error(f"Failed to process walk {walk.name}: {e}")

        # Create bundle
        logger.info("Creating optimized bundle...")
        bundle = create_bundle(processed_walks)

        # Compress with Brotli
        json_data = json.dumps(bundle, separators=(",", ":"))
        json_size = len(json_data.encode("utf-8"))

        brotli_data = brotli.compress(json_data.encode("utf-8"), quality=11)
        compressed_size = len(brotli_data)

        logger.info(
            f"Bundle sizes - Original: {json_size:,} bytes, Compressed: {compressed_size:,} bytes ({compressed_size / json_size * 100:.1f}%)"
        )

        # Count walks with viable windows
        walks_with_windows = sum(1 for pw in processed_walks if pw["windows"])
        total_windows = sum(len(pw["windows"]) for pw in processed_walks)

        logger.info(
            f"Generated bundle with {len(walks)} walks, {walks_with_windows} have viable windows, {total_windows} total windows"
        )

        # Upload to R2
        logger.info("Uploading bundle to R2...")
        uploader = S3Uploader()

        # Upload ONLY the Brotli-compressed version
        # NOTE: Do NOT set ContentEncoding="br" - that would cause R2 to auto-decompress.
        # Frontend manually decompresses, so we want R2 to serve the compressed file as-is.
        # Use .brotli extension instead of .br to prevent Cloudflare from auto-detecting
        # and adding content-encoding header (which causes double decompression).
        uploader.s3_client.put_object(
            Bucket=uploader.bucket_name,
            Key="bundle.brotli",
            Body=brotli_data,
            ContentType="application/octet-stream",  # Raw binary data
        )

        logger.info("Bundle successfully uploaded to R2 (Brotli version only)!")

    except Exception as e:
        logger.error(f"Failed to generate bundle: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
