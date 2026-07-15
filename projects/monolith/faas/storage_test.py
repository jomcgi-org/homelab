"""Tests for faas function archive storage (boto3 mocked; mirrors artifact/s3_test.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from faas import storage


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "seaweedfs-s3.seaweedfs.svc:8333")
    monkeypatch.setenv("FAAS_S3_BUCKET", "faas")


def _patch_client(monkeypatch, client: MagicMock):
    monkeypatch.setattr(storage, "_client", lambda: client)
    return client


def test_put_archive_writes_zip_object(monkeypatch):
    client = MagicMock()
    _patch_client(monkeypatch, client)

    storage.put_archive("echo-fn", "a" * 64, b"PK\x03\x04zipbytes")

    args = client.put_object.call_args.kwargs
    assert args["Bucket"] == "faas"
    assert args["Key"] == f"echo-fn/{'a' * 64}.zip"
    assert args["Body"] == b"PK\x03\x04zipbytes"
    assert args["ContentType"] == "application/zip"


def test_put_archive_creates_bucket_then_retries(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    err = ClientError({"Error": {"Code": "NoSuchBucket"}}, "PutObject")
    client.put_object.side_effect = [err, {}]
    _patch_client(monkeypatch, client)

    storage.put_archive("echo-fn", "a" * 64, b"zip")

    client.create_bucket.assert_called_once_with(Bucket="faas")
    assert client.put_object.call_count == 2


def test_head_archive_true_and_false(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.head_object.return_value = {}
    _patch_client(monkeypatch, client)
    assert storage.head_archive("echo-fn", "a" * 64) is True

    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404"}}, "HeadObject"
    )
    assert storage.head_archive("echo-fn", "a" * 64) is False


def test_get_archive_returns_bytes(monkeypatch):
    client = MagicMock()
    body = MagicMock()
    body.read.return_value = b"zipbytes"
    client.get_object.return_value = {"Body": body}
    _patch_client(monkeypatch, client)

    got = storage.get_archive("echo-fn", "a" * 64)

    assert got == b"zipbytes"
    assert client.get_object.call_args.kwargs["Key"] == f"echo-fn/{'a' * 64}.zip"


def test_get_archive_missing_returns_none(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "GetObject"
    )
    _patch_client(monkeypatch, client)

    assert storage.get_archive("echo-fn", "a" * 64) is None


def test_code_uri_matches_crd_sample_shape(monkeypatch):
    # This is the Helm-service-name form the CRD samples pin to
    # (projects/embervm/crd/samples/workload-echo-fn.yaml); production sets it
    # via values.yaml, never a hardcoded default (no-hardcoded-k8s-service-url).
    monkeypatch.setenv(
        "FAAS_S3_READ_BASE", "http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333"
    )

    uri = storage.code_uri("echo-fn", "a" * 64)

    assert uri == (
        "http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333"
        f"/faas/echo-fn/{'a' * 64}.zip"
    )


def test_code_uri_honors_read_base_override(monkeypatch):
    monkeypatch.setenv("FAAS_S3_READ_BASE", "http://custom-host:9000")

    uri = storage.code_uri("echo-fn", "a" * 64)

    assert uri == f"http://custom-host:9000/faas/echo-fn/{'a' * 64}.zip"


def test_code_uri_raises_when_read_base_unset(monkeypatch):
    monkeypatch.delenv("FAAS_S3_READ_BASE", raising=False)

    with pytest.raises(RuntimeError):
        storage.code_uri("echo-fn", "a" * 64)
