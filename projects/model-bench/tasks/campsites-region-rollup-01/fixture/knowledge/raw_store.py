"""S3 storage for raw ingest content (ADR 006 Phase 4c-4).

Raw markdown content is stored content-addressed at
``s3://knowledge/raws/<content_hash>.md`` on the SeaweedFS S3 gateway, with the
``knowledge.raw_inputs`` row holding the metadata. This replaces the old
file-based ``_raw/`` drop that relied on the (now offline) gardener move phase
to mirror files into Postgres.

The S3 client mirrors stars.grid._s3_client / chat.store._blob_s3_put: dummy
creds (SeaweedFS auth is disabled cluster-wide, but boto3 needs some value),
path-style addressing (SeaweedFS only supports it), and a scheme prefixed onto
the scheme-less endpoint the chart injects. When ``SEAWEEDFS_S3_ENDPOINT`` is
unset/empty (tests, local dev) the helpers no-op with a logged warning rather
than guessing a cluster URL.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("monolith.knowledge.raw_store")

RAW_BUCKET = "knowledge"


def _raw_key(content_hash: str) -> str:
    """S3 object key for a raw input keyed by its content hash."""
    return f"raws/{content_hash}.md"


def _s3_client():
    """boto3 S3 client pointed at the SeaweedFS S3 gateway, or None.

    Returns None (logging a warning) when ``SEAWEEDFS_S3_ENDPOINT`` is
    unset/empty so tests and local dev no-op cleanly. Mirrors
    stars.grid._s3_client: dummy creds, path-style addressing, scheme prefixed
    onto the scheme-less host:port the chart injects.
    """
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if not endpoint:
        logger.warning("knowledge raw S3 not configured; skipping S3 access")
        return None
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    # Scheme guaranteed by the guard above; inline nosemgrep clears the
    # pre-commit boto3-endpoint-url-missing-scheme hook (the Bazel
    # main_semgrep_test, which ignores nosemgrep, is covered by exclude_rules
    # in projects/monolith/BUILD).
    return boto3.client(  # nosemgrep: boto3-endpoint-url-missing-scheme
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "duckdb"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "duckdb"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )


def upload_raw(content_hash: str, content: str) -> None:
    """Upload raw content to S3, idempotently. No-ops when S3 is unconfigured.

    Content-addressed, so a key that already exists (head_object hit) is left
    untouched. The bucket is auto-created on first write.
    """
    from botocore.exceptions import ClientError

    client = _s3_client()
    if client is None:
        return
    key = _raw_key(content_hash)
    try:
        client.head_object(Bucket=RAW_BUCKET, Key=key)
        return
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("404", "NoSuchKey", "NoSuchBucket", "NotFound"):
            raise
    body = content.encode("utf-8")
    try:
        client.put_object(
            Bucket=RAW_BUCKET, Key=key, Body=body, ContentType="text/markdown"
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchBucket", "404"):
            client.create_bucket(Bucket=RAW_BUCKET)
            client.put_object(
                Bucket=RAW_BUCKET, Key=key, Body=body, ContentType="text/markdown"
            )
        else:
            raise


def fetch_raw(content_hash: str) -> str | None:
    """Fetch raw content from S3 by content hash, or None if missing/unconfigured."""
    from botocore.exceptions import ClientError

    client = _s3_client()
    if client is None:
        return None
    key = _raw_key(content_hash)
    try:
        resp = client.get_object(Bucket=RAW_BUCKET, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NoSuchBucket", "NotFound"):
            return None
        raise
    return resp["Body"].read().decode("utf-8")
