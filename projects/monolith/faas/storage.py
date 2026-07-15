"""Function zip archive storage on the SeaweedFS S3 gateway (ADR 045).

Mirrors artifact/s3.py's client construction and auto-create-bucket-on-first-write
fallback (dummy creds, path-style addressing, scheme guard on the endpoint;
boto3 imported lazily inside each function so importing this module never pulls
boto3 into the public app's import closure).

Object key layout is ``<name>/<sha256>.zip`` under the ``faas`` bucket (one
key per function name + zip content hash; last-write-wins registration keeps
the old object around for one supersession, per the plan's Task 9 note, since
nothing here deletes superseded objects). noded is the reader: it GETs
``code_uri`` directly over the in-cluster SeaweedFS S3 endpoint, sha256-verifying
the bytes host-side never unpacks them (Standing decision 3).
"""

import os


def _bucket() -> str:
    return os.environ.get("FAAS_S3_BUCKET", "faas")


def _object_key(name: str, sha256: str) -> str:
    return f"{name}/{sha256}.zip"


def _read_base() -> str:
    """In-cluster SeaweedFS S3 read host:port that noded GETs archives from.

    No hardcoded default (semgrep no-hardcoded-k8s-service-url): a Helm release
    rename silently changes the service name prefix, so this must come from
    values.yaml via FAAS_S3_READ_BASE. The chart sets it to the SeaweedFS S3
    Helm-service-name form pinned by the CRD samples
    (projects/embervm/crd/samples/workload-echo-fn.yaml) and the echo fixture
    README (projects/embervm/runtimes/python/testdata/echo/README.md).
    """
    base = os.environ.get("FAAS_S3_READ_BASE", "")
    if not base:
        raise RuntimeError("FAAS_S3_READ_BASE unset; cannot build faas code_uri")
    return base


def _client():
    """Build a SeaweedFS S3 client (mirrors artifact/s3.py / trips.s3)."""
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if not endpoint:
        raise RuntimeError("SEAWEEDFS_S3_ENDPOINT unset; cannot store faas archive")
    # The chart injects a scheme-less host:port; boto3 requires a scheme and
    # SeaweedFS S3 is plaintext HTTP.
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    # Scheme guaranteed above; clears the boto3-endpoint-url-missing-scheme hook
    # (see artifact/s3.py for the same nosemgrep + exclude_rules rationale).
    return boto3.client(  # nosemgrep: boto3-endpoint-url-missing-scheme
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "duckdb"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "duckdb"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )


def put_archive(name: str, sha256: str, data: bytes) -> None:
    """Write the function's zip bytes to ``s3://<bucket>/<name>/<sha256>.zip``.

    Creates the bucket once and retries on a missing bucket (mirrors
    artifact.s3.put_artifact / chat.store).
    """
    from botocore.exceptions import ClientError

    client = _client()
    bucket = _bucket()
    key = _object_key(name, sha256)

    def _put():
        return client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType="application/zip",
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


def head_archive(name: str, sha256: str) -> bool:
    """Return whether the archive object exists."""
    from botocore.exceptions import ClientError

    client = _client()
    try:
        client.head_object(Bucket=_bucket(), Key=_object_key(name, sha256))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            return False
        raise
    return True


def get_archive(name: str, sha256: str) -> bytes | None:
    """Return the archive's raw bytes, or None if it is absent."""
    from botocore.exceptions import ClientError

    client = _client()
    try:
        resp = client.get_object(Bucket=_bucket(), Key=_object_key(name, sha256))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            return None
        raise
    return resp["Body"].read()


def delete_archive(name: str, sha256: str) -> None:
    """Delete the archive object, ignoring a missing bucket/key.

    Best-effort teardown for the ingestion rollback / DELETE paths: a leaked
    object is harmless (the registry row is the authoritative record), so a
    NoSuchKey/NoSuchBucket is swallowed rather than raised.
    """
    from botocore.exceptions import ClientError

    client = _client()
    try:
        client.delete_object(Bucket=_bucket(), Key=_object_key(name, sha256))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            return
        raise


def code_uri(name: str, sha256: str) -> str:
    """Return the in-cluster read URL noded GETs for this archive.

    Built from FAAS_S3_READ_BASE (see ``_read_base``) + the bucket + object key,
    so a Workload CR's codeUri and this module always agree on the same URL
    shape. Raises if FAAS_S3_READ_BASE is unset.
    """
    return f"{_read_base()}/{_bucket()}/{_object_key(name, sha256)}"
