"""Tests for artifact session DB S3 storage (ADR 026 Phase 2).

Mirrors artifact/s3_test.py: boto3 is mocked via monkeypatch so the unit tests
run without a real SeaweedFS endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def test_put_session_writes_sessions_db_and_returns_clean_etag(monkeypatch):
    client = MagicMock()
    client.put_object.return_value = {"ETag": '"abc123"'}
    _patch_client(monkeypatch, client)

    etag = s3.put_session("demo", b"SQLite format 3")

    assert etag == "abc123"  # quotes stripped
    args = client.put_object.call_args.kwargs
    assert args["Bucket"] == "artifacts"
    assert args["Key"] == "demo/sessions.db"
    assert args["Body"] == b"SQLite format 3"
    assert args["ContentType"] == "application/octet-stream"


def test_put_session_creates_bucket_then_retries(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    err = ClientError({"Error": {"Code": "NoSuchBucket"}}, "PutObject")
    client.put_object.side_effect = [err, {"ETag": '"e"'}]
    _patch_client(monkeypatch, client)

    etag = s3.put_session("demo", b"x")

    assert etag == "e"
    client.create_bucket.assert_called_once_with(Bucket="artifacts")
    assert client.put_object.call_count == 2


def test_get_session_returns_bytes(monkeypatch):
    client = MagicMock()
    body = MagicMock()
    body.read.return_value = b"SQLite format 3\x00"
    client.get_object.return_value = {"Body": body, "ETag": '"v1"'}
    _patch_client(monkeypatch, client)

    got = s3.get_session("demo")

    assert got == b"SQLite format 3\x00"
    assert client.get_object.call_args.kwargs["Key"] == "demo/sessions.db"


def test_get_session_absent_returns_none(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "GetObject"
    )
    _patch_client(monkeypatch, client)

    assert s3.get_session("missing") is None


def test_get_session_absent_bucket_returns_none(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucket"}}, "GetObject"
    )
    _patch_client(monkeypatch, client)

    assert s3.get_session("missing") is None


def test_head_session_returns_etag(monkeypatch):
    client = MagicMock()
    client.head_object.return_value = {"ETag": '"v2"'}
    _patch_client(monkeypatch, client)

    assert s3.head_session("demo") == "v2"
    assert client.head_object.call_args.kwargs["Key"] == "demo/sessions.db"


def test_head_session_absent_returns_none(monkeypatch):
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404"}}, "HeadObject"
    )
    _patch_client(monkeypatch, client)

    assert s3.head_session("gone") is None


def test_session_key_uses_session_object_constant(monkeypatch):
    """_session_key must use _SESSION_OBJECT so a rename stays consistent."""
    assert s3._session_key("abc") == f"abc/{s3._SESSION_OBJECT}"
    assert s3._SESSION_OBJECT == "sessions.db"


# ---------------------------------------------------------------------------
# prune_sessions (ADR 026 Phase 2 Task 2.5: session TTL eviction)
# ---------------------------------------------------------------------------


def test_prune_sessions_deletes_old_session_objects_only(monkeypatch):
    """Only old sessions.db objects are deleted; new ones and non-session keys are left."""
    from botocore.exceptions import ClientError

    client = MagicMock()
    now = datetime.now(timezone.utc)
    old_ts = now - timedelta(days=31)
    new_ts = now - timedelta(days=1)

    # Three objects: one stale session, one fresh session, one artifact html.
    pages = [
        {
            "Contents": [
                {"Key": "abc/sessions.db", "LastModified": old_ts},
                {"Key": "xyz/sessions.db", "LastModified": new_ts},
                {"Key": "abc/index.html", "LastModified": old_ts},
            ]
        }
    ]
    paginator = MagicMock()
    paginator.paginate.return_value = iter(pages)
    client.get_paginator.return_value = paginator
    _patch_client(monkeypatch, client)

    deleted = s3.prune_sessions(max_age_days=30)

    assert deleted == 1
    client.delete_object.assert_called_once_with(
        Bucket="artifacts", Key="abc/sessions.db"
    )


def test_prune_sessions_no_such_bucket_returns_zero(monkeypatch):
    """A NoSuchBucket error means the bucket is empty; return 0 without re-raising."""
    from botocore.exceptions import ClientError

    client = MagicMock()
    err = ClientError({"Error": {"Code": "NoSuchBucket"}}, "ListObjectsV2")
    paginator = MagicMock()
    paginator.paginate.side_effect = err
    client.get_paginator.return_value = paginator
    _patch_client(monkeypatch, client)

    assert s3.prune_sessions(max_age_days=30) == 0
