"""Artifact storage on the SeaweedFS S3 gateway (ADR 024 + ADR 026).

goosecracker artifacts are agent-built, self-contained HTML pages published to
the ``artifacts`` bucket at ``<id>/index.html``. The monolith mediates every
write (the agent guest holds no S3 credential, ADR 024 decision 3), so this
module is the single S3 seam: ``put_artifact`` on the write path (full monolith)
and ``get_artifact`` / ``head_artifact`` on the public read path (the public
backend, proxied by the SSR frontend).

ADR 026 Phase 2 adds session DB storage at ``<id>/sessions.db`` so goose can
resume an existing run on a Discord thread reply: ``put_session`` stores the
SQLite bytes, ``get_session`` retrieves them, and ``head_session`` probes
existence cheaply.  All three are write-path (full monolith only); the session
DB is never served publicly.

The client construction mirrors ``trips.s3`` / ``stars.grid._s3_client`` (dummy
creds, path-style addressing, scheme guard on the endpoint) and the put +
auto-create-bucket fallback mirrors ``chat.store._blob_s3_put``. boto3 is
imported lazily inside each function so importing this module never pulls boto3
into the public app's import closure (app/main_public_imports_test.py).
"""

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("monolith.artifact.s3")

# Public-image-safe: a single object key per artifact id. Index naming keeps the
# door open for multi-file artifacts later without changing the id space.
_OBJECT_SUFFIX = "index.html"

# Session DB object key per artifact id (ADR 026 Phase 2). Stored alongside the
# artifact HTML in the same bucket so both objects share the same id namespace
# and bucket lifecycle.
_SESSION_OBJECT = "sessions.db"


def _bucket() -> str:
    return os.environ.get("ARTIFACTS_S3_BUCKET", "artifacts")


def _object_key(artifact_id: str) -> str:
    return f"{artifact_id}/{_OBJECT_SUFFIX}"


def _session_key(artifact_id: str) -> str:
    return f"{artifact_id}/{_SESSION_OBJECT}"


def _client():
    """Build a SeaweedFS S3 client (mirrors trips.s3 / stars.grid)."""
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if not endpoint:
        raise RuntimeError("SEAWEEDFS_S3_ENDPOINT unset; cannot store artifact")
    # The chart injects a scheme-less host:port (shared with DuckDB's httpfs);
    # boto3 requires a scheme and SeaweedFS S3 is plaintext HTTP.
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    # Scheme guaranteed above; the inline nosemgrep clears the pre-commit
    # boto3-endpoint-url-missing-scheme hook (the Bazel semgrep test ignores
    # nosemgrep and is covered by exclude_rules in projects/monolith/BUILD).
    return boto3.client(  # nosemgrep: boto3-endpoint-url-missing-scheme
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "duckdb"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "duckdb"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )


def put_artifact(artifact_id: str, html: bytes) -> str:
    """Write the artifact HTML to ``s3://<bucket>/<id>/index.html``.

    Overwrites any existing object under the id (re-publishing the same id is how
    an iterated artifact hot-reloads, ADR 024). Returns the stored object's ETag,
    which is the version the read path serves to the hot-reload poller. Creates
    the bucket once and retries on a missing bucket (mirrors chat.store).
    """
    from botocore.exceptions import ClientError

    client = _client()
    bucket = _bucket()
    key = _object_key(artifact_id)

    def _put():
        return client.put_object(
            Bucket=bucket,
            Key=key,
            Body=html,
            ContentType="text/html; charset=utf-8",
        )

    try:
        resp = _put()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchBucket", "404"):
            client.create_bucket(Bucket=bucket)
            resp = _put()
        else:
            raise
    return _clean_etag(resp.get("ETag", ""))


def get_artifact(artifact_id: str) -> tuple[bytes, str] | None:
    """Return ``(html_bytes, etag)`` for the artifact, or None if it is absent."""
    from botocore.exceptions import ClientError

    client = _client()
    try:
        resp = client.get_object(Bucket=_bucket(), Key=_object_key(artifact_id))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            return None
        raise
    body = resp["Body"].read()
    return body, _clean_etag(resp.get("ETag", ""))


def head_artifact(artifact_id: str) -> str | None:
    """Return the artifact's current ETag (version), or None if it is absent."""
    from botocore.exceptions import ClientError

    client = _client()
    try:
        resp = client.head_object(Bucket=_bucket(), Key=_object_key(artifact_id))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            return None
        raise
    return _clean_etag(resp.get("ETag", ""))


def _clean_etag(etag: str) -> str:
    """S3 ETags are quoted; strip the quotes for a clean version token."""
    return etag.strip('"')


def put_session(artifact_id: str, db: bytes) -> str:
    """Write the goose session DB to ``s3://<bucket>/<id>/sessions.db``.

    Overwrites any existing session for the id (re-storing after each run keeps
    the DB current so the next reply resumes from the latest state, ADR 026
    Phase 2). Returns the stored object's ETag. Creates the bucket once and
    retries on a missing bucket, mirroring ``put_artifact``.
    """
    from botocore.exceptions import ClientError

    client = _client()
    bucket = _bucket()
    key = _session_key(artifact_id)

    def _put():
        return client.put_object(
            Bucket=bucket,
            Key=key,
            Body=db,
            ContentType="application/octet-stream",
        )

    try:
        resp = _put()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchBucket", "404"):
            client.create_bucket(Bucket=bucket)
            resp = _put()
        else:
            raise
    return _clean_etag(resp.get("ETag", ""))


def get_session(artifact_id: str) -> bytes | None:
    """Return the raw session DB bytes, or None if no session exists yet."""
    from botocore.exceptions import ClientError

    client = _client()
    try:
        resp = client.get_object(Bucket=_bucket(), Key=_session_key(artifact_id))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            return None
        raise
    return resp["Body"].read()


def head_session(artifact_id: str) -> str | None:
    """Return the session DB's current ETag, or None if no session exists yet.

    Lets the orchestrator cheaply decide resume vs cold-start without downloading
    the full DB (ADR 026 Phase 2).
    """
    from botocore.exceptions import ClientError

    client = _client()
    try:
        resp = client.head_object(Bucket=_bucket(), Key=_session_key(artifact_id))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            return None
        raise
    return _clean_etag(resp.get("ETag", ""))


def prune_sessions(max_age_days: int = 30) -> int:
    """Delete persisted goose session DBs older than ``max_age_days`` (ADR 026
    Phase 2 Task 2.5: session TTL / eviction).

    Only session objects (``<id>/sessions.db``) are evicted, never the published
    artifacts (``<id>/index.html``): the artifact is the served output and must
    persist, while a session just makes the next reply resumable. Evicting a stale
    session is therefore safe: the next reply on that thread cleanly falls back to
    a cold Model-B rebuild (which re-seeds the session). Returns the count deleted.
    """
    from botocore.exceptions import ClientError

    client = _client()
    bucket = _bucket()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    suffix = "/" + _SESSION_OBJECT
    deleted = 0
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                if not key.endswith(suffix):
                    continue
                if obj.get("LastModified") and obj["LastModified"] < cutoff:
                    client.delete_object(Bucket=bucket, Key=key)
                    deleted += 1
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchBucket", "404"):
            return deleted
        raise
    return deleted
