"""Artifact storage on the SeaweedFS S3 gateway.

Artifacts are agent-built, self-contained HTML pages published to
the ``artifacts`` bucket at ``<id>/index.html``. The monolith mediates every
write (the agent guest holds no S3 credential, ADR 024 decision 3), so this
module is the single S3 seam: ``put_artifact`` on the write path (full monolith)
and ``get_artifact`` / ``head_artifact`` on the public read path (the public
backend, proxied by the SSR frontend).

The client construction mirrors ``trips.s3`` / ``stars.grid._s3_client`` (dummy
creds, path-style addressing, scheme guard on the endpoint) and the put +
auto-create-bucket fallback mirrors ``chat.store._blob_s3_put``. boto3 is
imported lazily inside each function so importing this module never pulls boto3
into the public app's import closure (app/main_public_imports_test.py).
"""

import logging
import os

logger = logging.getLogger("monolith.artifact.s3")

# Public-image-safe: a single object key per artifact id. Index naming keeps the
# door open for multi-file artifacts later without changing the id space.
_OBJECT_SUFFIX = "index.html"


def _bucket() -> str:
    return os.environ.get("ARTIFACTS_S3_BUCKET", "artifacts")


def _object_key(artifact_id: str) -> str:
    return f"{artifact_id}/{_OBJECT_SUFFIX}"


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
