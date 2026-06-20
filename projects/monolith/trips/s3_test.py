"""Unit tests for trips.s3.put_image -- trip image upload to SeaweedFS.

All tests patch boto3 at the call site so no real S3 endpoint is needed.
boto3 is imported inside put_image (lazy import) so the module-level import
of trips.s3 succeeds without boto3 on the path; the @pip//boto3 dep is only
needed for the ClientError import and for the mock to resolve the patch target.

Covers:
  - Missing endpoint env var raises RuntimeError immediately
  - Scheme is prepended when endpoint is scheme-less (host:port)
  - Existing http:// / https:// scheme not doubled
  - Head-object hit: object already exists, put skipped
  - Head-object 404 / NoSuchKey: object missing, put_object called
  - put_object raises NoSuchBucket: bucket auto-created, put retried
  - Unexpected ClientError from head_object re-raised
  - Unexpected ClientError from put_object re-raised
  - Bucket name read from TRIPS_S3_BUCKET env var
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from trips.s3 import put_image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "op")


def _make_s3(
    *,
    head_side_effect=None,
    head_return_value=None,
    put_side_effect=None,
) -> MagicMock:
    s3 = MagicMock()
    if head_side_effect is not None:
        s3.head_object.side_effect = head_side_effect
    if head_return_value is not None:
        s3.head_object.return_value = head_return_value
    if put_side_effect is not None:
        s3.put_object.side_effect = put_side_effect
    return s3


# ---------------------------------------------------------------------------
# Missing endpoint
# ---------------------------------------------------------------------------


class TestMissingEndpoint:
    def test_raises_when_env_var_unset(self, monkeypatch):
        monkeypatch.delenv("SEAWEEDFS_S3_ENDPOINT", raising=False)
        with pytest.raises(RuntimeError, match="SEAWEEDFS_S3_ENDPOINT unset"):
            put_image("img_abc.jpg", b"\xff\xd8", "image/jpeg")

    def test_raises_when_env_var_empty_string(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "")
        with pytest.raises(RuntimeError, match="SEAWEEDFS_S3_ENDPOINT unset"):
            put_image("img_abc.jpg", b"\xff\xd8", "image/jpeg")


# ---------------------------------------------------------------------------
# Scheme guard
# ---------------------------------------------------------------------------


class TestSchemeGuard:
    def test_http_prefix_added_for_bare_host_port(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "seaweed:8333")
        s3 = _make_s3(head_return_value={"ETag": "abc"})

        with patch("boto3.client", return_value=s3) as mock_factory:  # nosemgrep
            put_image("img_k.jpg", b"data", "image/jpeg")

        _, kwargs = mock_factory.call_args
        assert kwargs["endpoint_url"] == "http://seaweed:8333"

    def test_existing_http_scheme_not_doubled(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://seaweed:8333")
        s3 = _make_s3(head_return_value={"ETag": "abc"})

        with patch("boto3.client", return_value=s3) as mock_factory:  # nosemgrep
            put_image("img_k.jpg", b"data", "image/jpeg")

        _, kwargs = mock_factory.call_args
        assert kwargs["endpoint_url"] == "http://seaweed:8333"

    def test_existing_https_scheme_not_modified(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "https://seaweed:8333")
        s3 = _make_s3(head_return_value={"ETag": "abc"})

        with patch("boto3.client", return_value=s3) as mock_factory:  # nosemgrep
            put_image("img_k.jpg", b"data", "image/jpeg")

        _, kwargs = mock_factory.call_args
        assert kwargs["endpoint_url"] == "https://seaweed:8333"


# ---------------------------------------------------------------------------
# Head-object hit: skip put
# ---------------------------------------------------------------------------


class TestHeadObjectHit:
    def test_existing_object_skips_put(self, monkeypatch):
        """Content-addressed: head_object hit means the bytes are already stored."""
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://seaweed:8333")
        s3 = _make_s3(head_return_value={"ETag": "existing"})

        with patch("boto3.client", return_value=s3):  # nosemgrep
            put_image("img_existing.jpg", b"bytes", "image/jpeg")

        s3.put_object.assert_not_called()


# ---------------------------------------------------------------------------
# Head-object miss: put_object called
# ---------------------------------------------------------------------------


class TestHeadObjectMiss:
    def test_nosuchkey_triggers_put(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://seaweed:8333")
        s3 = _make_s3(head_side_effect=_client_error("NoSuchKey"))

        with patch("boto3.client", return_value=s3):  # nosemgrep
            put_image("img_new.jpg", b"newbytes", "image/jpeg")

        s3.put_object.assert_called_once()
        kw = s3.put_object.call_args.kwargs
        assert kw["Key"] == "img_new.jpg"
        assert kw["Body"] == b"newbytes"
        assert kw["ContentType"] == "image/jpeg"

    def test_404_on_head_triggers_put(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://seaweed:8333")
        s3 = _make_s3(head_side_effect=_client_error("404"))

        with patch("boto3.client", return_value=s3):  # nosemgrep
            put_image("img_new.jpg", b"data", "image/jpeg")

        s3.put_object.assert_called_once()

    def test_nosuchbucket_on_head_triggers_put(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://seaweed:8333")
        s3 = _make_s3(head_side_effect=_client_error("NoSuchBucket"))

        with patch("boto3.client", return_value=s3):  # nosemgrep
            put_image("img_new.jpg", b"data", "image/jpeg")

        s3.put_object.assert_called_once()


# ---------------------------------------------------------------------------
# Auto-create bucket
# ---------------------------------------------------------------------------


class TestAutoCreateBucket:
    def test_nosuchbucket_on_put_creates_bucket_then_retries(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://seaweed:8333")
        s3 = MagicMock()
        s3.head_object.side_effect = _client_error("NoSuchKey")
        s3.put_object.side_effect = [_client_error("NoSuchBucket"), None]

        with patch("boto3.client", return_value=s3):  # nosemgrep
            put_image("img_nob.jpg", b"data", "image/jpeg")

        s3.create_bucket.assert_called_once()
        assert s3.put_object.call_count == 2

    def test_404_on_put_same_as_nosuchbucket(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://seaweed:8333")
        s3 = MagicMock()
        s3.head_object.side_effect = _client_error("NoSuchKey")
        s3.put_object.side_effect = [_client_error("404"), None]

        with patch("boto3.client", return_value=s3):  # nosemgrep
            put_image("img_404b.jpg", b"data", "image/jpeg")

        s3.create_bucket.assert_called_once()


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    def test_unexpected_head_error_is_re_raised(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://seaweed:8333")
        s3 = _make_s3(head_side_effect=_client_error("AccessDenied"))

        with patch("boto3.client", return_value=s3):  # nosemgrep
            with pytest.raises(ClientError):
                put_image("img_denied.jpg", b"data", "image/jpeg")

    def test_unexpected_put_error_is_re_raised(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://seaweed:8333")
        s3 = MagicMock()
        s3.head_object.side_effect = _client_error("NoSuchKey")
        s3.put_object.side_effect = _client_error("InternalError")

        with patch("boto3.client", return_value=s3):  # nosemgrep
            with pytest.raises(ClientError):
                put_image("img_ierr.jpg", b"data", "image/jpeg")


# ---------------------------------------------------------------------------
# Bucket name from env var
# ---------------------------------------------------------------------------


class TestBucketNameEnvVar:
    def test_custom_bucket_name_used(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://seaweed:8333")
        monkeypatch.setenv("TRIPS_S3_BUCKET", "custom-bucket")
        s3 = _make_s3(head_return_value={})

        with patch("boto3.client", return_value=s3):  # nosemgrep
            put_image("img_k.jpg", b"data", "image/jpeg")

        kw = s3.head_object.call_args.kwargs
        assert kw["Bucket"] == "custom-bucket"

    def test_default_bucket_name_is_monolith_trips(self, monkeypatch):
        monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://seaweed:8333")
        monkeypatch.delenv("TRIPS_S3_BUCKET", raising=False)
        s3 = _make_s3(head_return_value={})

        with patch("boto3.client", return_value=s3):  # nosemgrep
            put_image("img_k.jpg", b"data", "image/jpeg")

        kw = s3.head_object.call_args.kwargs
        assert kw["Bucket"] == "monolith-trips"
