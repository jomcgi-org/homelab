"""Tests for publish-trip-images/main.py — core scan/queue/EXIF helpers.

Covers:
- dms_to_decimal: GPS DMS-to-decimal conversion
- get_gps_info: EXIF GPS tag extraction
- calculate_light_value: LV from exposure triangle
- format_shutter_speed: shutter speed string formatting
- scan_images: filesystem image scanner
- generate_dest_key: deterministic local dedup-key generation
- sample_images_by_time: time-interval image sampling
- UploadQueue: CRUD operations (add, get_pending, get_completed,
  mark_uploading, mark_completed, mark_failed, reset_uploading, get_stats)

The HTTP ingestion POST path is covered by ingest_post_test.py.
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from main import (
    OpticsData,
    UploadQueue,
    UploadStatus,
    calculate_light_value,
    dms_to_decimal,
    format_shutter_speed,
    generate_dest_key,
    get_gps_info,
    sample_images_by_time,
    scan_images,
)


# ---------------------------------------------------------------------------
# dms_to_decimal
# ---------------------------------------------------------------------------


class TestDmsToDecimal:
    """Convert GPS DMS tuples to decimal degrees."""

    def test_north_latitude_positive(self):
        # 49°16'58.4"N = 49 + 16/60 + 58.4/3600 ≈ 49.282889
        result = dms_to_decimal((49, 16, 58.4), "N")
        assert abs(result - 49.28288888) < 0.0001

    def test_south_latitude_negative(self):
        result = dms_to_decimal((33, 52, 0), "S")
        assert result < 0
        assert abs(result - -33.8666666) < 0.0001

    def test_east_longitude_positive(self):
        result = dms_to_decimal((123, 7, 18.0), "E")
        assert result > 0

    def test_west_longitude_negative(self):
        result = dms_to_decimal((123, 7, 18.0), "W")
        assert result < 0

    def test_zero_zero_returns_zero(self):
        assert dms_to_decimal((0, 0, 0), "N") == 0.0

    def test_exact_degrees_no_minutes_seconds(self):
        result = dms_to_decimal((45, 0, 0), "N")
        assert result == 45.0

    def test_minutes_conversion(self):
        # 0°30'0" = 0.5 degrees
        result = dms_to_decimal((0, 30, 0), "N")
        assert abs(result - 0.5) < 1e-9

    def test_seconds_conversion(self):
        # 0°0'3600" = 1 degree
        result = dms_to_decimal((0, 0, 3600), "N")
        assert abs(result - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# get_gps_info
# ---------------------------------------------------------------------------


class TestGetGpsInfo:
    """Convert numeric EXIF GPS tag IDs to human-readable names."""

    def test_returns_dict_with_named_keys(self):
        from PIL.ExifTags import GPSTAGS

        # Use a known GPS tag ID (GPSLatitudeRef = 1)
        gps_lat_ref_id = next(k for k, v in GPSTAGS.items() if v == "GPSLatitudeRef")
        raw = {gps_lat_ref_id: "N"}
        result = get_gps_info(raw)
        assert "GPSLatitudeRef" in result
        assert result["GPSLatitudeRef"] == "N"

    def test_unknown_tag_id_preserved_as_int(self):
        raw = {99999: "unknown_value"}
        result = get_gps_info(raw)
        assert 99999 in result

    def test_empty_input_returns_empty_dict(self):
        assert get_gps_info({}) == {}

    def test_multiple_gps_tags(self):
        from PIL.ExifTags import GPSTAGS

        lat_id = next(k for k, v in GPSTAGS.items() if v == "GPSLatitude")
        lon_id = next(k for k, v in GPSTAGS.items() if v == "GPSLongitude")
        raw = {lat_id: (49, 16, 58), lon_id: (123, 7, 18)}
        result = get_gps_info(raw)
        assert "GPSLatitude" in result
        assert "GPSLongitude" in result


# ---------------------------------------------------------------------------
# calculate_light_value
# ---------------------------------------------------------------------------


class TestCalculateLightValue:
    """LV = log2(N²/t) - log2(ISO/100)."""

    def test_typical_values(self):
        # f/2.8, 1/250s, ISO 100
        # LV = log2(2.8^2 / (1/250)) - log2(100/100) = log2(1960) - 0 ≈ 10.9
        result = calculate_light_value(2.8, 1 / 250, 100)
        assert result is not None
        assert 10 < result < 12

    def test_returns_none_when_aperture_is_none(self):
        assert calculate_light_value(None, 1 / 100, 200) is None

    def test_returns_none_when_shutter_is_none(self):
        assert calculate_light_value(2.8, None, 200) is None

    def test_returns_none_when_iso_is_none(self):
        assert calculate_light_value(2.8, 1 / 100, None) is None

    def test_returns_none_for_zero_aperture(self):
        assert calculate_light_value(0, 1 / 100, 200) is None

    def test_returns_none_for_zero_shutter(self):
        assert calculate_light_value(2.8, 0, 200) is None

    def test_returns_none_for_zero_iso(self):
        assert calculate_light_value(2.8, 1 / 100, 0) is None

    def test_returns_none_for_negative_aperture(self):
        assert calculate_light_value(-1.4, 1 / 100, 200) is None

    def test_result_is_rounded_to_one_decimal(self):
        result = calculate_light_value(2.8, 1 / 250, 100)
        assert result == round(result, 1)

    def test_higher_iso_decreases_lv(self):
        lv_low = calculate_light_value(2.8, 1 / 250, 100)
        lv_high = calculate_light_value(2.8, 1 / 250, 3200)
        assert lv_high < lv_low


# ---------------------------------------------------------------------------
# format_shutter_speed
# ---------------------------------------------------------------------------


class TestFormatShutterSpeed:
    """Format exposure time float to human-readable string."""

    def test_fast_shutter_as_fraction(self):
        # 1/240 s
        result = format_shutter_speed(1 / 240)
        assert result == "1/240"

    def test_one_second_exposure(self):
        result = format_shutter_speed(1.0)
        assert result == "1.0s"

    def test_long_exposure(self):
        result = format_shutter_speed(30.0)
        assert result == "30.0s"

    def test_one_over_60(self):
        result = format_shutter_speed(1 / 60)
        assert result == "1/60"

    def test_returns_none_for_none(self):
        assert format_shutter_speed(None) is None

    def test_returns_none_for_zero(self):
        assert format_shutter_speed(0) is None

    def test_returns_none_for_negative(self):
        assert format_shutter_speed(-0.01) is None

    def test_half_second(self):
        result = format_shutter_speed(0.5)
        # 0.5 < 1, so 1/round(1/0.5) = 1/2
        assert result == "1/2"


# ---------------------------------------------------------------------------
# scan_images
# ---------------------------------------------------------------------------


class TestScanImages:
    """Recursive image scanner sorted by mtime."""

    def test_finds_jpg_files(self, tmp_path):
        (tmp_path / "a.jpg").write_bytes(b"img")
        images = scan_images(tmp_path)
        assert any(p.name == "a.jpg" for p in images)

    def test_finds_jpeg_extension(self, tmp_path):
        (tmp_path / "b.jpeg").write_bytes(b"img")
        images = scan_images(tmp_path)
        assert any(p.name == "b.jpeg" for p in images)

    def test_finds_png_files(self, tmp_path):
        (tmp_path / "c.png").write_bytes(b"img")
        images = scan_images(tmp_path)
        assert any(p.name == "c.png" for p in images)

    def test_finds_heic_files(self, tmp_path):
        (tmp_path / "d.heic").write_bytes(b"img")
        images = scan_images(tmp_path)
        assert any(p.name == "d.heic" for p in images)

    def test_finds_heif_files(self, tmp_path):
        (tmp_path / "e.heif").write_bytes(b"img")
        images = scan_images(tmp_path)
        assert any(p.name == "e.heif" for p in images)

    def test_excludes_non_image_files(self, tmp_path):
        (tmp_path / "doc.txt").write_bytes(b"text")
        (tmp_path / "video.mp4").write_bytes(b"vid")
        images = scan_images(tmp_path)
        names = [p.name for p in images]
        assert "doc.txt" not in names
        assert "video.mp4" not in names

    def test_excludes_macos_resource_forks(self, tmp_path):
        (tmp_path / "._photo.jpg").write_bytes(b"rsrc")
        (tmp_path / "photo.jpg").write_bytes(b"img")
        images = scan_images(tmp_path)
        names = [p.name for p in images]
        assert "._photo.jpg" not in names
        assert "photo.jpg" in names

    def test_scans_subdirectories_recursively(self, tmp_path):
        sub = tmp_path / "DCIM" / "100GOPRO"
        sub.mkdir(parents=True)
        (sub / "shot.jpg").write_bytes(b"img")
        images = scan_images(tmp_path)
        assert any(p.name == "shot.jpg" for p in images)

    def test_empty_directory_returns_empty_list(self, tmp_path):
        assert scan_images(tmp_path) == []

    def test_case_insensitive_extension(self, tmp_path):
        (tmp_path / "IMG.JPG").write_bytes(b"img")
        images = scan_images(tmp_path)
        assert any(p.name == "IMG.JPG" for p in images)

    def test_returns_path_objects(self, tmp_path):
        (tmp_path / "x.jpg").write_bytes(b"img")
        images = scan_images(tmp_path)
        assert all(isinstance(p, Path) for p in images)


# ---------------------------------------------------------------------------
# generate_dest_key
# ---------------------------------------------------------------------------


class TestGenerateDestKey:
    """Deterministic S3 key generation from source + timestamp + filename."""

    def test_key_starts_with_img_(self):
        key = generate_dest_key(Path("photo.jpg"), "gopro", "2025-07-01T12:00:00")
        assert key.startswith("img_")

    def test_key_ends_with_jpg(self):
        key = generate_dest_key(Path("shot.jpg"), "gopro", "2025-07-01T12:00:00")
        assert key.endswith(".jpg")

    def test_deterministic_same_inputs(self):
        key1 = generate_dest_key(Path("photo.jpg"), "gopro", "2025-07-01T12:00:00")
        key2 = generate_dest_key(Path("photo.jpg"), "gopro", "2025-07-01T12:00:00")
        assert key1 == key2

    def test_different_source_different_key(self):
        key_gopro = generate_dest_key(Path("photo.jpg"), "gopro", "2025-07-01T12:00:00")
        key_phone = generate_dest_key(Path("photo.jpg"), "phone", "2025-07-01T12:00:00")
        assert key_gopro != key_phone

    def test_different_timestamp_different_key(self):
        key1 = generate_dest_key(Path("photo.jpg"), "gopro", "2025-07-01T12:00:00")
        key2 = generate_dest_key(Path("photo.jpg"), "gopro", "2025-07-01T12:00:01")
        assert key1 != key2

    def test_different_filename_different_key(self):
        key1 = generate_dest_key(Path("a.jpg"), "gopro", "2025-07-01T12:00:00")
        key2 = generate_dest_key(Path("b.jpg"), "gopro", "2025-07-01T12:00:00")
        assert key1 != key2

    def test_heic_converted_to_jpg_extension(self):
        key = generate_dest_key(Path("photo.heic"), "gopro", "2025-07-01T12:00:00")
        assert key.endswith(".jpg")

    def test_heif_converted_to_jpg_extension(self):
        key = generate_dest_key(Path("photo.heif"), "gopro", "2025-07-01T12:00:00")
        assert key.endswith(".jpg")

    def test_png_extension_preserved(self):
        key = generate_dest_key(Path("photo.png"), "gopro", "2025-07-01T12:00:00")
        assert key.endswith(".png")

    def test_no_timestamp_still_generates_key(self):
        key = generate_dest_key(Path("photo.jpg"), "gopro", None)
        assert key.startswith("img_")
        assert key.endswith(".jpg")

    def test_key_hex_is_12_chars(self):
        key = generate_dest_key(Path("photo.jpg"), "gopro", "2025-07-01T12:00:00")
        # "img_" + 12 hex chars + ".jpg"
        basename = key[4:].rsplit(".", 1)[0]
        assert len(basename) == 12
        assert all(c in "0123456789abcdef" for c in basename)


# ---------------------------------------------------------------------------
# UploadQueue
# ---------------------------------------------------------------------------


class TestUploadQueueInit:
    """Database schema is created on construction."""

    def test_creates_images_table(self, tmp_path):
        q = UploadQueue(tmp_path / "q.db")
        with sqlite3.connect(q.db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='images'"
            ).fetchall()
        assert len(rows) == 1

    def test_creates_status_index(self, tmp_path):
        q = UploadQueue(tmp_path / "q.db")
        with sqlite3.connect(q.db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_status'"
            ).fetchall()
        assert len(rows) == 1

    def test_idempotent_init(self, tmp_path):
        """Constructing twice on the same DB must not raise."""
        db = tmp_path / "q.db"
        UploadQueue(db)
        UploadQueue(db)


class TestUploadQueueAdd:
    """Adding records to the upload queue."""

    @pytest.fixture
    def queue(self, tmp_path):
        return UploadQueue(tmp_path / "q.db")

    def test_returns_integer_id(self, queue, tmp_path):
        rid = queue.add(
            tmp_path / "photo.jpg", "img_abc.jpg", 49.0, -123.0, "2025-07-01T12:00:00"
        )
        assert isinstance(rid, int)
        assert rid >= 1

    def test_autoincrement_ids(self, queue, tmp_path):
        id1 = queue.add(tmp_path / "a.jpg", "img_aaa.jpg", None, None, None)
        id2 = queue.add(tmp_path / "b.jpg", "img_bbb.jpg", None, None, None)
        assert id2 == id1 + 1

    def test_duplicate_dest_key_returns_none(self, queue, tmp_path):
        queue.add(tmp_path / "a.jpg", "img_abc.jpg", None, None, None)
        result = queue.add(tmp_path / "b.jpg", "img_abc.jpg", None, None, None)
        assert result is None

    def test_new_record_starts_as_pending(self, queue, tmp_path):
        queue.add(tmp_path / "a.jpg", "img_abc.jpg", None, None, None)
        pending = queue.get_pending()
        assert len(pending) == 1
        assert pending[0].status == UploadStatus.PENDING

    def test_stores_lat_lng(self, queue, tmp_path):
        queue.add(tmp_path / "a.jpg", "img_abc.jpg", 49.2827, -123.1207, None)
        pending = queue.get_pending()
        assert pending[0].lat == 49.2827
        assert pending[0].lng == -123.1207

    def test_stores_timestamp(self, queue, tmp_path):
        queue.add(tmp_path / "a.jpg", "img_abc.jpg", None, None, "2025-07-01T12:00:00")
        pending = queue.get_pending()
        assert pending[0].timestamp == "2025-07-01T12:00:00"

    def test_stores_tags(self, queue, tmp_path):
        queue.add(
            tmp_path / "a.jpg", "img_abc.jpg", None, None, None, tags=["wildlife"]
        )
        pending = queue.get_pending()
        assert pending[0].tags == ["wildlife"]

    def test_stores_optics(self, queue, tmp_path):
        optics = OpticsData(iso=400, aperture=2.8)
        queue.add(tmp_path / "a.jpg", "img_abc.jpg", None, None, None, optics=optics)
        pending = queue.get_pending()
        assert pending[0].optics is not None
        assert pending[0].optics.iso == 400
        assert pending[0].optics.aperture == 2.8


class TestUploadQueueStateTransitions:
    """mark_uploading / mark_completed / mark_failed."""

    @pytest.fixture
    def queue_with_record(self, tmp_path):
        q = UploadQueue(tmp_path / "q.db")
        rid = q.add(tmp_path / "photo.jpg", "img_abc.jpg", None, None, None)
        return q, rid

    def test_mark_uploading_changes_status(self, queue_with_record):
        q, rid = queue_with_record
        q.mark_uploading(rid)
        with sqlite3.connect(q.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM images WHERE id = ?", (rid,)
            ).fetchone()
        assert row[0] == UploadStatus.UPLOADING.value

    def test_mark_completed_changes_status(self, queue_with_record):
        q, rid = queue_with_record
        q.mark_completed(rid)
        with sqlite3.connect(q.db_path) as conn:
            row = conn.execute(
                "SELECT status, completed_at FROM images WHERE id = ?", (rid,)
            ).fetchone()
        assert row[0] == UploadStatus.COMPLETED.value
        assert row[1] is not None

    def test_mark_failed_changes_status_and_stores_error(self, queue_with_record):
        q, rid = queue_with_record
        q.mark_failed(rid, "network error")
        with sqlite3.connect(q.db_path) as conn:
            row = conn.execute(
                "SELECT status, error_message, retry_count FROM images WHERE id = ?",
                (rid,),
            ).fetchone()
        assert row[0] == UploadStatus.FAILED.value
        assert row[1] == "network error"
        assert row[2] == 1

    def test_mark_failed_increments_retry_count(self, queue_with_record):
        q, rid = queue_with_record
        q.mark_failed(rid, "err1")
        q.mark_failed(rid, "err2")
        with sqlite3.connect(q.db_path) as conn:
            row = conn.execute(
                "SELECT retry_count FROM images WHERE id = ?", (rid,)
            ).fetchone()
        assert row[0] == 2


class TestUploadQueueGetPending:
    """get_pending includes PENDING and retryable FAILED, not COMPLETED."""

    @pytest.fixture
    def queue(self, tmp_path):
        return UploadQueue(tmp_path / "q.db")

    def test_empty_queue_returns_empty_list(self, queue):
        assert queue.get_pending() == []

    def test_pending_record_returned(self, queue, tmp_path):
        queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        assert len(queue.get_pending()) == 1

    def test_completed_record_not_returned(self, queue, tmp_path):
        rid = queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        queue.mark_completed(rid)
        assert queue.get_pending() == []

    def test_uploading_record_not_returned(self, queue, tmp_path):
        rid = queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        queue.mark_uploading(rid)
        # Uploading is in-progress — not in pending
        pending = queue.get_pending()
        assert all(r.id != rid for r in pending)

    def test_failed_within_retry_limit_returned(self, queue, tmp_path):
        rid = queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        queue.mark_failed(rid, "err")
        assert any(r.id == rid for r in queue.get_pending())

    def test_failed_at_max_retries_not_returned(self, queue, tmp_path):
        rid = queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        for _ in range(UploadQueue.MAX_RETRIES):
            queue.mark_failed(rid, "err")
        assert all(r.id != rid for r in queue.get_pending())

    def test_results_ordered_by_id_ascending(self, queue, tmp_path):
        ids = [
            queue.add(tmp_path / f"{i}.jpg", f"img_{i}.jpg", None, None, None)
            for i in range(3)
        ]
        pending = queue.get_pending()
        assert [r.id for r in pending] == ids


class TestUploadQueueGetCompleted:
    """get_completed returns only COMPLETED records."""

    @pytest.fixture
    def queue(self, tmp_path):
        return UploadQueue(tmp_path / "q.db")

    def test_empty_queue_returns_empty_list(self, queue):
        assert queue.get_completed() == []

    def test_completed_record_returned(self, queue, tmp_path):
        rid = queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        queue.mark_completed(rid)
        completed = queue.get_completed()
        assert len(completed) == 1
        assert completed[0].status == UploadStatus.COMPLETED

    def test_pending_record_not_in_completed(self, queue, tmp_path):
        queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        assert queue.get_completed() == []


class TestUploadQueueResetUploading:
    """Reset interrupted uploads to pending."""

    @pytest.fixture
    def queue(self, tmp_path):
        return UploadQueue(tmp_path / "q.db")

    def test_uploading_records_reset_to_pending(self, queue, tmp_path):
        rid = queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        queue.mark_uploading(rid)
        count = queue.reset_uploading()
        assert count == 1
        pending = queue.get_pending()
        assert any(r.id == rid for r in pending)

    def test_returns_number_of_records_reset(self, queue, tmp_path):
        ids = [
            queue.add(tmp_path / f"{i}.jpg", f"img_{i}.jpg", None, None, None)
            for i in range(3)
        ]
        for rid in ids:
            queue.mark_uploading(rid)
        assert queue.reset_uploading() == 3

    def test_no_uploading_records_returns_zero(self, queue, tmp_path):
        queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        assert queue.reset_uploading() == 0

    def test_completed_records_not_affected(self, queue, tmp_path):
        rid = queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        queue.mark_completed(rid)
        queue.reset_uploading()
        assert queue.get_completed()[0].status == UploadStatus.COMPLETED


class TestUploadQueueGetStats:
    """Status count aggregation."""

    @pytest.fixture
    def queue(self, tmp_path):
        return UploadQueue(tmp_path / "q.db")

    def test_empty_queue_returns_empty_dict(self, queue):
        assert queue.get_stats() == {}

    def test_counts_by_status(self, queue, tmp_path):
        id1 = queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        id2 = queue.add(tmp_path / "b.jpg", "img_b.jpg", None, None, None)
        queue.add(tmp_path / "c.jpg", "img_c.jpg", None, None, None)
        queue.mark_completed(id1)
        queue.mark_failed(id2, "err")
        stats = queue.get_stats()
        assert stats[UploadStatus.COMPLETED.value] == 1
        assert stats[UploadStatus.FAILED.value] == 1
        assert stats[UploadStatus.PENDING.value] == 1

    def test_only_present_statuses_included(self, queue, tmp_path):
        queue.add(tmp_path / "a.jpg", "img_a.jpg", None, None, None)
        stats = queue.get_stats()
        assert UploadStatus.COMPLETED.value not in stats


# ---------------------------------------------------------------------------
# sample_images_by_time
# ---------------------------------------------------------------------------


class TestSampleImagesByTime:
    """Image sampling: at least one image per interval_seconds."""

    def _make_images(self, tmp_path, count: int) -> list[Path]:
        imgs = []
        for i in range(count):
            p = tmp_path / f"img_{i:03d}.jpg"
            p.write_bytes(b"img")
            imgs.append(p)
        return imgs

    def test_zero_interval_returns_all_images(self, tmp_path):
        """interval_seconds=0 means no sampling — return all images."""
        imgs = self._make_images(tmp_path, 5)
        with patch("main.extract_exif", return_value=(49.0, -123.0, None, None)):
            result = sample_images_by_time(imgs, 0)
        assert len(result) == 5

    def test_first_image_always_selected(self, tmp_path):
        imgs = self._make_images(tmp_path, 3)
        timestamps = [
            "2025-07-01T12:00:00",
            "2025-07-01T12:00:05",
            "2025-07-01T12:01:00",
        ]
        calls = [(49.0, -123.0, ts, None) for ts in timestamps]
        with patch("main.extract_exif", side_effect=calls):
            result = sample_images_by_time(imgs, 60)
        # First image should be selected
        assert result[0][0] == imgs[0]

    def test_selects_image_after_interval_elapsed(self, tmp_path):
        imgs = self._make_images(tmp_path, 3)
        timestamps = [
            "2025-07-01T12:00:00",
            "2025-07-01T12:00:30",  # only 30s → not enough
            "2025-07-01T12:01:00",  # 60s from first → selected
        ]
        calls = [(49.0, -123.0, ts, None) for ts in timestamps]
        with patch("main.extract_exif", side_effect=calls):
            result = sample_images_by_time(imgs, 60)
        selected_paths = [r[0] for r in result]
        assert imgs[0] in selected_paths
        assert imgs[1] not in selected_paths
        assert imgs[2] in selected_paths

    def test_empty_input_returns_empty(self):
        result = sample_images_by_time([], 60)
        assert result == []

    def test_images_without_timestamp_skipped_after_first(self, tmp_path):
        """Images with no timestamp are skipped (prefer timestamped images)."""
        imgs = self._make_images(tmp_path, 3)
        calls = [
            (49.0, -123.0, "2025-07-01T12:00:00", None),  # first — selected
            (49.0, -123.0, None, None),  # no timestamp — skipped
            (49.0, -123.0, "2025-07-01T12:01:00", None),  # 60s — selected
        ]
        with patch("main.extract_exif", side_effect=calls):
            result = sample_images_by_time(imgs, 60)
        selected = [r[0] for r in result]
        assert imgs[1] not in selected

    def test_result_tuples_have_5_elements(self, tmp_path):
        imgs = self._make_images(tmp_path, 1)
        with patch(
            "main.extract_exif",
            return_value=(49.0, -123.0, "2025-07-01T12:00:00", None),
        ):
            result = sample_images_by_time(imgs, 0)
        assert len(result) == 1
        assert len(result[0]) == 5  # (path, lat, lng, timestamp, optics)
