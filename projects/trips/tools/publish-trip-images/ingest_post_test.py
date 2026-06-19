"""Tests for the HTTP ingestion POST path in publish-trip-images/main.py.

Exercises ``_run_upload`` / ``post_image`` / ``ingest_config`` against a MOCKED
httpx client (no real network):

- 201 -> record dequeued (marked completed).
- 422 (no GPS) -> permanent skip (dequeued, never retried).
- 500 / network error -> retryable (left pending in the queue).
- the POST carries the raw JPEG multipart + trip/source/tags.
- Cloudflare Access headers are added only when the env vars are set.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio  # noqa: F401 — registers the asyncio plugin

from main import (
    UploadQueue,
    UploadStatus,
    _run_upload,
    ingest_config,
    post_image,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(status_code: int, text: str = "") -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.text = text
    return r


def _fake_client(response=None, exc=None) -> MagicMock:
    """Build a mock standing in for httpx.AsyncClient (async context manager)."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if exc is not None:
        client.post = AsyncMock(side_effect=exc)
    else:
        client.post = AsyncMock(return_value=response)
    return client


@pytest.fixture
def env(monkeypatch):
    """Local port-forward style env: ingest URL only, no Cloudflare headers."""
    monkeypatch.setenv("TRIPS_INGEST_URL", "https://example.test")
    monkeypatch.delenv("CF_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)


@pytest.fixture
def seeded(tmp_path):
    """A queue DB with one pending record pointing at a real (dummy) JPEG file."""
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0 fake jpeg bytes")
    db = tmp_path / "queue.db"
    queue = UploadQueue(db)
    rid = queue.add(img, "img_seed.jpg", 49.0, -123.0, "2025-07-01T12:00:00")
    # Empty source dir so _run_upload's scan adds nothing new.
    empty = tmp_path / "empty"
    empty.mkdir()
    return db, queue, rid, empty, img


# ---------------------------------------------------------------------------
# ingest_config
# ---------------------------------------------------------------------------


class TestIngestConfig:
    def test_requires_url(self, monkeypatch):
        monkeypatch.delenv("TRIPS_INGEST_URL", raising=False)
        with pytest.raises(RuntimeError):
            ingest_config()

    def test_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("TRIPS_INGEST_URL", "https://example.test/")
        base, _ = ingest_config()
        assert base == "https://example.test"

    def test_omits_cf_headers_when_unset(self, env):
        _, headers = ingest_config()
        assert "CF-Access-Client-Id" not in headers
        assert "CF-Access-Client-Secret" not in headers

    def test_adds_cf_headers_when_both_set(self, monkeypatch):
        monkeypatch.setenv("TRIPS_INGEST_URL", "https://example.test")
        monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "cf-id")
        monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "cf-secret")
        _, headers = ingest_config()
        assert headers["CF-Access-Client-Id"] == "cf-id"
        assert headers["CF-Access-Client-Secret"] == "cf-secret"

    def test_omits_cf_headers_when_only_one_set(self, monkeypatch):
        monkeypatch.setenv("TRIPS_INGEST_URL", "https://example.test")
        monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "cf-id")
        monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)
        _, headers = ingest_config()
        assert "CF-Access-Client-Id" not in headers


# ---------------------------------------------------------------------------
# post_image
# ---------------------------------------------------------------------------


class TestPostImage:
    @pytest.mark.asyncio
    async def test_sends_raw_jpeg_multipart_and_params(self, tmp_path):
        img = tmp_path / "shot.jpg"
        img.write_bytes(b"raw-jpeg-bytes")
        db = tmp_path / "q.db"
        queue = UploadQueue(db)
        queue.add(img, "img_x.jpg", 1.0, 2.0, None, tags=["wildlife", "hotspring"])
        record = queue.get_pending()[0]

        client = _fake_client(_resp(201))
        await post_image(
            client,
            "https://example.test",
            {},
            record,
            trip="van-2025",
            source="gopro",
        )

        args, kwargs = client.post.call_args
        assert args[0] == "https://example.test/api/trips/ingest"
        assert kwargs["params"] == {
            "trip": "van-2025",
            "source": "gopro",
            "tags": "wildlife,hotspring",
        }
        # multipart 'image' field carries the raw bytes read from disk.
        field_name, (filename, data, content_type) = next(iter(kwargs["files"].items()))
        assert field_name == "image"
        assert filename == "shot.jpg"
        assert data == b"raw-jpeg-bytes"
        assert content_type == "image/jpeg"


# ---------------------------------------------------------------------------
# _run_upload — dequeue / skip / retry behaviour
# ---------------------------------------------------------------------------


class TestRunUploadOutcomes:
    @pytest.mark.asyncio
    async def test_201_marks_completed_and_dequeues(self, env, seeded):
        db, queue, rid, empty, _ = seeded
        client = _fake_client(_resp(201, ""))
        with patch("main.httpx.AsyncClient", return_value=client):
            await _run_upload(empty, db, trip="t", dry_run=False)

        assert queue.get_pending() == []
        completed = queue.get_completed()
        assert len(completed) == 1
        assert completed[0].id == rid
        client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_422_marks_skipped_and_not_retried(self, env, seeded):
        db, queue, rid, empty, _ = seeded
        client = _fake_client(_resp(422, "no gps"))
        with patch("main.httpx.AsyncClient", return_value=client):
            await _run_upload(empty, db, trip="t", dry_run=False)

        # Skipped is terminal: not pending, not completed.
        assert queue.get_pending() == []
        assert queue.get_completed() == []
        assert queue.get_stats().get(UploadStatus.SKIPPED.value) == 1

        # A second run must not POST it again (permanent skip).
        client2 = _fake_client(_resp(201))
        with patch("main.httpx.AsyncClient", return_value=client2):
            await _run_upload(empty, db, trip="t", dry_run=False)
        client2.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_500_marks_failed_and_stays_pending(self, env, seeded):
        db, queue, rid, empty, _ = seeded
        client = _fake_client(_resp(500, "boom"))
        with patch("main.httpx.AsyncClient", return_value=client):
            await _run_upload(empty, db, trip="t", dry_run=False)

        pending = queue.get_pending()
        assert len(pending) == 1
        assert pending[0].id == rid
        assert pending[0].status == UploadStatus.FAILED
        assert pending[0].retry_count == 1

    @pytest.mark.asyncio
    async def test_network_error_marks_failed_and_stays_pending(self, env, seeded):
        db, queue, rid, empty, _ = seeded
        client = _fake_client(exc=httpx.ConnectError("connection refused"))
        with patch("main.httpx.AsyncClient", return_value=client):
            await _run_upload(empty, db, trip="t", dry_run=False)

        pending = queue.get_pending()
        assert len(pending) == 1
        assert pending[0].status == UploadStatus.FAILED
        assert pending[0].retry_count == 1

    @pytest.mark.asyncio
    async def test_dry_run_does_not_post(self, env, seeded):
        db, queue, rid, empty, _ = seeded
        client = _fake_client(_resp(201))
        with patch("main.httpx.AsyncClient", return_value=client):
            await _run_upload(empty, db, trip="t", dry_run=True)

        client.post.assert_not_awaited()
        # Still pending after a dry run.
        assert len(queue.get_pending()) == 1

    @pytest.mark.asyncio
    async def test_post_carries_trip_and_auth(self, env, seeded):
        db, queue, rid, empty, _ = seeded
        client = _fake_client(_resp(201))
        with patch("main.httpx.AsyncClient", return_value=client):
            await _run_upload(empty, db, trip="van-2025", dry_run=False, source="phone")

        _, kwargs = client.post.call_args
        assert kwargs["params"]["trip"] == "van-2025"
        assert kwargs["params"]["source"] == "phone"
        assert "CF-Access-Client-Id" not in kwargs["headers"]
        assert Path(next(iter(kwargs["files"].values()))[0]).suffix == ".jpg"
