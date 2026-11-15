"""Screenshot storage contracts without storage access."""

import hashlib
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

from core.s3_upload import put_object
from shotter import s3


@pytest.fixture(autouse=True)
def storage_env(monkeypatch):
    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "https://storage.example/")
    monkeypatch.setenv("SHOTTER_S3_BUCKET", "screenshots")


def test_shared_upload_arguments_and_derived_url(monkeypatch):
    assert s3.put_object is put_object
    client = Mock()
    monkeypatch.setattr(s3, "_client", lambda: client)
    body = b"png"
    key = f"{hashlib.sha256(body).hexdigest()}.png"
    with patch.object(s3, "put_object") as upload:
        assert s3.put_screenshot(body) == (
            f"https://storage.example/screenshots/{key}",
            True,
        )
    upload.assert_called_once_with(
        client, bucket="screenshots", key=key, body=body, content_type="image/png"
    )


@pytest.mark.parametrize("code", ["NoSuchBucket", "404"])
def test_missing_bucket_retries(monkeypatch, code):
    client = Mock()
    client.put_object.side_effect = [
        ClientError({"Error": {"Code": code}}, "PutObject"),
        {},
    ]
    monkeypatch.setattr(s3, "_client", lambda: client)
    assert s3.put_screenshot(b"png")[1] is True
    client.create_bucket.assert_called_once_with(Bucket="screenshots")
    assert client.put_object.call_count == 2


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject"),
        RuntimeError("offline"),
    ],
)
def test_upload_failure_keeps_url_and_reports_not_stored(monkeypatch, caplog, error):
    client = Mock()
    client.put_object.side_effect = error
    monkeypatch.setattr(s3, "_client", lambda: client)
    key = f"{hashlib.sha256(b'png').hexdigest()}.png"
    assert s3.put_screenshot(b"png") == (
        f"https://storage.example/screenshots/{key}",
        False,
    )
    assert "S3 upload failed" in caplog.text
    client.create_bucket.assert_not_called()


def test_unconfigured_returns_fallback_url_and_not_stored(monkeypatch):
    monkeypatch.delenv("SEAWEEDFS_S3_ENDPOINT")
    monkeypatch.delenv("SHOTTER_S3_BUCKET")
    with patch.object(s3, "path_style_client") as client:
        assert s3.put_screenshot(b"png") == (
            f"shotter/{hashlib.sha256(b'png').hexdigest()}.png",
            False,
        )
    client.assert_not_called()
