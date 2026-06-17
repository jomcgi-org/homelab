"""Unit tests for knowledge.raw_store (fileless raw S3 storage, ADR 006)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from knowledge.raw_store import RAW_BUCKET, _raw_key, fetch_raw, upload_raw


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "Op")


def test_raw_key_is_content_addressed():
    assert _raw_key("abc123") == "raws/abc123.md"


class TestUploadRaw:
    def test_noop_when_no_s3_client(self):
        """Unconfigured S3 (no client) is a clean no-op, not an error."""
        with patch("knowledge.raw_store._s3_client", return_value=None):
            # Should not raise.
            upload_raw("hash1", "content")

    def test_idempotent_head_hit_skips_put(self):
        """An existing object (head_object succeeds) is not re-uploaded."""
        client = MagicMock()
        client.head_object.return_value = {}
        with patch("knowledge.raw_store._s3_client", return_value=client):
            upload_raw("hash1", "content")
        client.head_object.assert_called_once_with(
            Bucket=RAW_BUCKET, Key="raws/hash1.md"
        )
        client.put_object.assert_not_called()

    def test_puts_when_head_misses(self):
        """A missing object (head 404) triggers a put_object."""
        client = MagicMock()
        client.head_object.side_effect = _client_error("404")
        with patch("knowledge.raw_store._s3_client", return_value=client):
            upload_raw("hash2", "the body")
        client.put_object.assert_called_once_with(
            Bucket=RAW_BUCKET,
            Key="raws/hash2.md",
            Body=b"the body",
            ContentType="text/markdown",
        )

    def test_creates_bucket_when_missing(self):
        """A NoSuchBucket on put triggers create_bucket + retry."""
        client = MagicMock()
        client.head_object.side_effect = _client_error("NoSuchBucket")
        client.put_object.side_effect = [_client_error("NoSuchBucket"), None]
        with patch("knowledge.raw_store._s3_client", return_value=client):
            upload_raw("hash3", "body")
        client.create_bucket.assert_called_once_with(Bucket=RAW_BUCKET)
        assert client.put_object.call_count == 2


class TestFetchRaw:
    def test_returns_none_when_no_client(self):
        with patch("knowledge.raw_store._s3_client", return_value=None):
            assert fetch_raw("hash1") is None

    def test_returns_decoded_content(self):
        client = MagicMock()
        client.get_object.return_value = {"Body": MagicMock(read=lambda: b"hello")}
        with patch("knowledge.raw_store._s3_client", return_value=client):
            assert fetch_raw("hash1") == "hello"
        client.get_object.assert_called_once_with(
            Bucket=RAW_BUCKET, Key="raws/hash1.md"
        )

    def test_returns_none_on_missing_key(self):
        client = MagicMock()
        client.get_object.side_effect = _client_error("NoSuchKey")
        with patch("knowledge.raw_store._s3_client", return_value=client):
            assert fetch_raw("missing") is None
