"""Tests for artifact S3 storage (boto3 mocked; mirrors trips/s3_test.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from artifact import s3


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "seaweedfs-s3.seaweedfs.svc:8333")
    monkeypatch.setenv("ARTIFACTS_S3_BUCKET", "artifacts")


def _patch_client(monkeypatch, client: MagicMock):
    monkeypatch.setattr(s3, "_client", lambda: client)
    return client


def test_get_artifact_returns_body_and_etag(monkeypatch):
    client = MagicMock()
    body = MagicMock()
    body.read.return_value = b"<p>hello</p>"
    client.get_object.return_value = {"Body": body, "ETag": '"v1"'}
    _patch_client(monkeypatch, client)

    got = s3.get_artifact("demo")

    assert got == (b"<p>hello</p>", "v1")
    client.get_object.assert_called_once_with(Bucket="artifacts", Key="demo/index.html")


@pytest.mark.parametrize("code", ["NoSuchKey", "NoSuchBucket", "404"])
@pytest.mark.parametrize("operation", ["get", "head"])
def test_missing_artifact_returns_none(monkeypatch, code, operation):
    from botocore.exceptions import ClientError

    client = MagicMock()
    getattr(client, f"{operation}_object").side_effect = ClientError(
        {"Error": {"Code": code}}, f"{operation.title()}Object"
    )
    _patch_client(monkeypatch, client)

    assert getattr(s3, f"{operation}_artifact")("missing") is None
    assert client.mock_calls == [
        getattr(call, f"{operation}_object")(
            Bucket="artifacts", Key="missing/index.html"
        )
    ]


def test_head_artifact_returns_etag_or_none(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.head_object.return_value = {"ETag": '"v2"'}
    _patch_client(monkeypatch, client)
    assert s3.head_artifact("demo") == "v2"
    client.head_object.assert_called_once_with(
        Bucket="artifacts", Key="demo/index.html"
    )

    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404"}}, "HeadObject"
    )
    assert s3.head_artifact("gone") is None


@pytest.mark.parametrize("code", ["AccessDenied", "InternalError"])
@pytest.mark.parametrize("operation", ["get", "head"])
def test_storage_errors_propagate(monkeypatch, code, operation):
    from botocore.exceptions import ClientError

    client = _patch_client(monkeypatch, MagicMock())
    error = ClientError({"Error": {"Code": code}}, f"{operation.title()}Object")
    getattr(client, f"{operation}_object").side_effect = error

    with pytest.raises(ClientError) as caught:
        getattr(s3, f"{operation}_artifact")("demo")
    assert caught.value is error
    client.put_object.assert_not_called()
    client.create_bucket.assert_not_called()
