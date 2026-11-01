"""Read existing artifacts on Cloudflare R2 through its S3 API.

Artifacts are agent-built, self-contained HTML pages published to
the ``artifacts`` bucket at ``<id>/index.html``. ``get_artifact`` and
``head_artifact`` serve the read path on both backends, proxied by the SSR
frontend.

The client comes from ``core.s3`` (chart credentials, path-style addressing,
endpoint scheme guard). boto3 is imported lazily inside each function so
importing this module never pulls boto3 into the public app's import closure
(app/main_public_imports_test.py).
"""

import os

from core.s3 import path_style_client

# Public-image-safe: a single object key per artifact id. Index naming keeps the
# door open for multi-file artifacts later without changing the id space.
_OBJECT_SUFFIX = "index.html"


def _bucket() -> str:
    return os.environ.get("ARTIFACTS_S3_BUCKET", "artifacts")


def _object_key(artifact_id: str) -> str:
    return f"{artifact_id}/{_OBJECT_SUFFIX}"


def _client():
    """Build an R2 S3 client for the artifacts bucket."""
    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if not endpoint:
        raise RuntimeError("SEAWEEDFS_S3_ENDPOINT unset; cannot read artifact")
    return path_style_client(endpoint)


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
