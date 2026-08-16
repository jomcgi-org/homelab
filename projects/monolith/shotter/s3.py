"""Screenshot PNG storage on the SeaweedFS S3 gateway.

A sibling of artifact/s3.py, not a reuse: artifact hardcodes an
``index.html`` object suffix and ``text/html`` content type for a
self-contained HTML page, this domain stores a PNG under a content-addressed
key instead. The client construction mirrors artifact/s3.py and
faas/storage.py (dummy creds, path-style addressing, scheme guard on the
endpoint). boto3 is imported lazily inside each function so importing this
module never pulls boto3 into the public app's import closure (this domain
is excluded from the public binary entirely, but the convention is kept for
consistency with the domains that are).

Upload is best-effort, mirroring chat.store._blob_s3_put: the returned URL is
computed from the PNG's content hash before the upload is even attempted, so
the MCP tool can still hand back a real ImageContent block with the rendered
PNG bytes already embedded even when the SeaweedFS write itself fails or is
unavailable. The stored URL is a durable reference for later lookup, not the
only way to get at the image; the tool's caller already has the bytes.
"""

from __future__ import annotations

import hashlib
import logging
import os

logger = logging.getLogger("monolith.shotter.s3")


def _bucket() -> str:
    return os.environ.get("SHOTTER_S3_BUCKET", "shotter")


def _object_key(sha256: str) -> str:
    return f"{sha256}.png"


def _normalized_endpoint() -> str:
    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if endpoint and not endpoint.startswith(("http://", "https://")):
        # The chart injects a scheme-less host:port; boto3 requires a scheme
        # and SeaweedFS S3 is plaintext HTTP.
        endpoint = "http://" + endpoint
    return endpoint


def _client():
    """Build a SeaweedFS S3 client (mirrors artifact/s3.py / faas/storage.py)."""
    import boto3
    from botocore.config import Config

    endpoint = _normalized_endpoint()
    if not endpoint:
        raise RuntimeError("SEAWEEDFS_S3_ENDPOINT unset; cannot store screenshot")
    # Scheme guaranteed by _normalized_endpoint; the inline nosemgrep clears
    # the pre-commit boto3-endpoint-url-missing-scheme hook (the Bazel
    # semgrep test, which ignores nosemgrep, is covered by exclude_rules in
    # projects/monolith/BUILD).
    return boto3.client(  # nosemgrep: boto3-endpoint-url-missing-scheme
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "duckdb"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "duckdb"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )


def _object_url(bucket: str, key: str) -> str:
    """Build the object's URL from the configured S3 endpoint.

    Falls back to a bare ``bucket/key`` path when SEAWEEDFS_S3_ENDPOINT is
    unset (e.g. a hermetic unit test), so a missing or unreachable endpoint
    never blocks the caller from getting back a non-empty, well-formed URL
    string for the object this call was about to write.
    """
    endpoint = _normalized_endpoint()
    if not endpoint:
        return f"{bucket}/{key}"
    return f"{endpoint.rstrip('/')}/{bucket}/{key}"


def put_screenshot(png_bytes: bytes) -> tuple[str, bool]:
    """Best-effort upload of a captured screenshot PNG.

    Returns ``(url, stored)``. ``url`` is always populated because it is derived
    from the content hash rather than from the write, and ``stored`` says whether
    an object actually exists behind it. Callers must surface ``stored``: the
    upload is deliberately non-fatal so a SeaweedFS blip does not discard a
    screenshot the caller can already see, but a URL that 404s must never be
    indistinguishable from one that resolves.

    Content-addressed at ``s3://<bucket>/<sha256>.png``, so re-capturing an
    unchanged render dedupes for free. Creates the bucket once and retries on
    a missing bucket (mirrors artifact.s3.put_artifact / chat.store).
    """
    from botocore.exceptions import ClientError

    sha256 = hashlib.sha256(png_bytes).hexdigest()
    bucket = _bucket()
    key = _object_key(sha256)
    url = _object_url(bucket, key)

    try:
        client = _client()

        def _put():
            return client.put_object(
                Bucket=bucket,
                Key=key,
                Body=png_bytes,
                ContentType="image/png",
            )

        try:
            _put()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchBucket", "404"):
                client.create_bucket(Bucket=bucket)
                _put()
            else:
                raise
    except Exception:  # noqa: BLE001 - upload is best-effort, see module docstring
        logger.exception("shotter screenshot S3 upload failed for %s", key)
        # Best-effort, but never silent. The URL is derived from the content
        # hash before the upload is attempted, so on this path it names an
        # object that does not exist. Returning it without saying so would hand
        # the caller a link that 404s with no way to tell that from a working
        # one, which is worse than having no link at all. The caller decides
        # what to do; it already holds the PNG bytes either way.
        return url, False

    return url, True
