"""Tests for artifact S3 storage (boto3 mocked; mirrors trips/s3_test.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from artifact import s3


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "seaweedfs-s3.seaweedfs.svc:8333")
    monkeypatch.setenv("ARTIFACTS_S3_BUCKET", "artifacts")


def _patch_client(monkeypatch, client: MagicMock):
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = client
    monkeypatch.setattr(s3, "_client", lambda: client)
    return client


def test_put_artifact_writes_index_html_and_returns_clean_etag(monkeypatch):
    client = MagicMock()
    client.put_object.return_value = {"ETag": '"abc123"'}
    _patch_client(monkeypatch, client)

    etag = s3.put_artifact("demo", b"<h1>hi</h1>")

    assert etag == "abc123"  # quotes stripped
    args = client.put_object.call_args.kwargs
    assert args["Bucket"] == "artifacts"
    assert args["Key"] == "demo/index.html"
    assert args["Body"] == b"<h1>hi</h1>"
    assert args["ContentType"].startswith("text/html")


def test_put_artifact_creates_bucket_then_retries(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    err = ClientError({"Error": {"Code": "NoSuchBucket"}}, "PutObject")
    client.put_object.side_effect = [err, {"ETag": '"e"'}]
    _patch_client(monkeypatch, client)

    etag = s3.put_artifact("demo", b"x")

    assert etag == "e"
    client.create_bucket.assert_called_once_with(Bucket="artifacts")
    assert client.put_object.call_count == 2


def test_get_artifact_returns_body_and_etag(monkeypatch):
    client = MagicMock()
    body = MagicMock()
    body.read.return_value = b"<p>hello</p>"
    client.get_object.return_value = {"Body": body, "ETag": '"v1"'}
    _patch_client(monkeypatch, client)

    got = s3.get_artifact("demo")

    assert got == (b"<p>hello</p>", "v1")
    assert client.get_object.call_args.kwargs["Key"] == "demo/index.html"


def test_get_artifact_missing_returns_none(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "GetObject"
    )
    _patch_client(monkeypatch, client)

    assert s3.get_artifact("missing") is None


def test_head_artifact_returns_etag_or_none(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.head_object.return_value = {"ETag": '"v2"'}
    _patch_client(monkeypatch, client)
    assert s3.head_artifact("demo") == "v2"

    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404"}}, "HeadObject"
    )
    assert s3.head_artifact("gone") is None
