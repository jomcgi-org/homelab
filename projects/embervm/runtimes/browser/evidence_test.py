import base64
import hashlib
import json
import struct
import zlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from evidence import (
    CONSOLE_MAX_BYTES,
    CONSOLE_MAX_ENTRIES,
    DEFAULT_VIEWPORTS,
    NETWORK_MAX_BYTES,
    NETWORK_MAX_ENTRIES,
    PNG_SIGNATURE,
    EvidenceRecord,
    EvidenceRequirements,
    bounded_summary,
    capture_console,
    capture_network,
    image_blocks_for_review,
    verify_evidence,
)

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def png(viewport):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    width, height = viewport
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00" * width * 3) * height))
        + chunk(b"IEND", b"")
    )


def record(data, kind="screenshot", viewport=DEFAULT_VIEWPORTS[0]):
    return EvidenceRecord(
        kind=kind,
        source_url="https://preview.example.test/",
        app_commit="a" * 40,
        task_id="task-1",
        run_id="run-1",
        session_id="session-1",
        capture_time="2026-09-30T23:59:00Z",
        viewport=viewport if kind == "screenshot" else None,
        media_type="image/png" if kind == "screenshot" else "application/json",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        storage={"backend": "test", "uri": "artifact:" + hashlib.sha256(data).hexdigest()},
        expires_at="2026-10-02T00:00:00Z",
        link="https://artifacts.example.test/1",
        upload_status="uploaded",
    )


@pytest.fixture
def evidence():
    outcome = {
        "navigation": "passed",
        "viewports": {
            f"{w}x{h}": {
                "functional": {"functional": "passed"},
                "accessibility": {"accessibility": "passed"},
            }
            for w, h in DEFAULT_VIEWPORTS
        },
    }
    data = [png(v) for v in DEFAULT_VIEWPORTS] + [json.dumps(outcome).encode()]
    records = [record(d, viewport=v) for d, v in zip(data, DEFAULT_VIEWPORTS)]
    records.append(record(data[-1], kind="interaction_outcome"))
    return records, {r.sha256: d for r, d in zip(records, data)}, outcome


def verdict(records, blobs):
    return verify_evidence(records, None, NOW, lambda r: blobs.get(r.sha256))


def codes(result):
    assert result.status == "incomplete"
    return {reason["code"] for reason in result.reasons}


def test_complete(evidence):
    records, blobs, _ = evidence
    assert verdict(records, blobs).status == "complete"


def test_missing_viewport(evidence):
    records, blobs, _ = evidence
    assert "screenshot_missing" in codes(verdict(records[1:], blobs))


@pytest.mark.parametrize("failure", ["missing", "exception"])
def test_unavailable_artifact(evidence, failure):
    records, _, _ = evidence

    def fetch(_):
        if failure == "exception":
            raise OSError("unavailable")
        return None

    expected = "artifact_missing" if failure == "missing" else "fetch_failed"
    assert expected in codes(verify_evidence(records, None, NOW, fetch))


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"expires_at": "2026-10-01T00:00:00Z"}, "expired"),
        ({"sha256": "b" * 64}, "hash_mismatch"),
        ({"size_bytes": 1}, "size_mismatch"),
        ({"upload_status": "failed"}, "upload_failed"),
        ({"upload_status": "pending"}, "upload_pending"),
        ({"media_type": "text/plain"}, "invalid_png"),
    ],
)
def test_artifact_failure(evidence, changes, expected):
    records, blobs, _ = evidence
    original = records[0]
    records[0] = replace(original, **changes)
    # Fetch the original bytes even when the declared digest is wrong.
    assert expected in codes(
        verify_evidence(records, None, NOW, lambda r: blobs[original.sha256] if r is records[0] else blobs[r.sha256])
    )


@pytest.mark.parametrize(
    "data, expected",
    [(b"not a png", "invalid_png"), (png((1, 1)), "png_dimensions_mismatch")],
)
def test_png_failures(evidence, data, expected):
    records, blobs, _ = evidence
    records[0] = record(data)
    blobs[records[0].sha256] = data
    assert expected in codes(verdict(records, blobs))


@pytest.mark.parametrize("category", ["functional", "accessibility"])
@pytest.mark.parametrize("status, expected", [("skipped", "check_skipped"), ("errored", "check_errored"), ("failed", "check_failed"), (None, "check_missing"), ([], "check_invalid")])
def test_check_failures(evidence, category, status, expected):
    records, blobs, outcome = evidence
    outcome["viewports"]["1440x900"][category][category] = status
    data = json.dumps(outcome).encode()
    records[-1] = record(data, kind="interaction_outcome")
    blobs[records[-1].sha256] = data
    assert expected in codes(verdict(records, blobs))


def test_failed_navigation(evidence):
    records, blobs, outcome = evidence
    outcome["navigation"] = "failed"
    data = json.dumps(outcome).encode()
    records[-1] = record(data, kind="interaction_outcome")
    blobs[records[-1].sha256] = data
    assert "navigation_failed" in codes(verdict(records, blobs))


def test_accessibility_is_not_visual(evidence):
    records, blobs, _ = evidence
    snapshots = [replace(r, kind="accessibility_snapshot", viewport=None) for r in records[:2]]
    assert "screenshot_missing" in codes(verdict(snapshots + records[2:], blobs))
    with pytest.raises(ValueError, match="screenshot_missing"):
        image_blocks_for_review(snapshots, lambda r: blobs[r.sha256], now=NOW)


@pytest.mark.parametrize("revision", ["A" * 40, "a" * 39, "g" * 40, "main", "a" * 40 + "\n"])
def test_bad_app_commit(revision):
    with pytest.raises(ValueError, match="app_commit"):
        replace(record(png((1, 1)), viewport=(1, 1)), app_commit=revision)


@pytest.mark.parametrize("capture, expiry", [("2026-09-30", "2026-10-02T00:00:00Z"), ("2026-09-30T00:00:00-01:00", "2026-10-02T00:00:00Z"), ("2026-09-30T00:00:00Z", "2026-09-29T00:00:00Z")])
def test_invalid_times(capture, expiry):
    with pytest.raises(ValueError):
        replace(record(png((1, 1)), viewport=(1, 1)), capture_time=capture, expires_at=expiry)


def test_wrong_identity(evidence):
    records, blobs, _ = evidence
    records[0] = replace(records[0], session_id="another-session")
    assert "identity_mismatch" in codes(verdict(records, blobs))


@pytest.mark.parametrize("capture, count, byte_limit", [(capture_console, CONSOLE_MAX_ENTRIES, CONSOLE_MAX_BYTES), (capture_network, NETWORK_MAX_ENTRIES, NETWORK_MAX_BYTES)])
def test_capture_bounds(capture, count, byte_limit):
    result = capture(["x"] * (count + 3))
    assert len(result.entries) == count
    assert result.truncated and result.dropped_count == 3
    result = capture(["é" * (byte_limit // 2), "x", "x" * (byte_limit + 1)])
    assert result.size_bytes == byte_limit
    assert sum(len(e.encode()) for e in result.entries) == byte_limit
    assert result.truncated and result.dropped_count == 2
    assert capture([]).dropped_count == 0
    assert not capture([{"status": 500}]).truncated


def test_review_receives_bytes(evidence):
    records, blobs, _ = evidence
    blocks = image_blocks_for_review(records, lambda r: blobs[r.sha256], now=NOW)
    assert len(blocks) == 2
    for block, rec in zip(blocks, records):
        assert block["source"]["media_type"] == "image/png"
        assert base64.b64decode(block["source"]["data"]) == blobs[rec.sha256]


@pytest.mark.parametrize("fetch", [lambda r: None, lambda r: r.link, lambda r: b"corrupt"])
def test_review_refuses_url_only_or_corrupt(evidence, fetch):
    with pytest.raises(ValueError, match="incomplete image evidence"):
        image_blocks_for_review(evidence[0], fetch, now=NOW)


def test_summary_bounds(evidence):
    records, blobs, _ = evidence
    result = verdict(records, blobs)
    full = bounded_summary(records, result, 10000)
    for rec in records:
        assert rec.kind in full and rec.sha256[:12] in full
        assert str(rec.size_bytes) in full and rec.expires_at in full and rec.link in full
    assert "1440x900" in full and "390x844" in full and "complete" in full
    for limit in (0, 1, 10, 100):
        assert len(bounded_summary(records, result, limit)) <= limit


def test_absent_outcome_and_invalid_requirements(evidence):
    records, blobs, _ = evidence
    assert {"outcome_missing", "navigation_missing", "check_missing"} <= codes(verdict(records[:2], blobs))
    with pytest.raises(ValueError):
        EvidenceRequirements(functional_checks=())
