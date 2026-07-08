"""Trip image storage on the SeaweedFS S3 gateway.

Trip photos are content-addressed (``img_<sha256[:12]>.jpg``) and uploaded to the
``monolith-trips`` bucket. The client construction mirrors stars.grid._s3_client
(dummy creds, path-style addressing, scheme guard on the endpoint) and the
idempotent put + auto-create-bucket fallback mirrors chat.store._blob_s3_put.

The bucket is also provisioned out of band via GitOps in a later task; the
auto-create here is a defensive fallback so a fresh environment self-heals.
"""

import logging
import os

logger = logging.getLogger("monolith.trips.s3")


def put_image(image_key: str, data: bytes, content_type: str) -> None:
    """Upload a trip image to the monolith-trips bucket, idempotently.

    Content-addressed: an existing object under ``image_key`` is identical
    bytes, so a present ``head_object`` short-circuits the put. On a missing
    bucket the bucket is created once and the put retried (mirrors
    chat.store._blob_s3_put). Raises if the endpoint is unconfigured: trip
    images have a read path (the SSR map renders them), so a silent miss would
    leave a broken point rather than an archival gap.
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if not endpoint:
        raise RuntimeError("SEAWEEDFS_S3_ENDPOINT unset; cannot store trip image")
    # The chart injects a scheme-less host:port (shared with DuckDB's httpfs).
    # boto3 requires a scheme on endpoint_url; SeaweedFS S3 is plaintext HTTP,
    # so prefix http:// when absent.
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    # Scheme guaranteed by the guard above; the inline nosemgrep clears the
    # pre-commit boto3-endpoint-url-missing-scheme hook (the Bazel
    # main_semgrep_test, which ignores nosemgrep, is covered by exclude_rules in
    # projects/monolith/BUILD).
    client = boto3.client(  # nosemgrep: boto3-endpoint-url-missing-scheme
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "duckdb"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "duckdb"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )

    bucket = os.environ.get("TRIPS_S3_BUCKET", "monolith-trips")

    # Content-addressed: identical key means identical bytes, so skip the put.
    try:
        client.head_object(Bucket=bucket, Key=image_key)
        return
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("NoSuchKey", "NoSuchBucket", "404"):
            raise

    try:
        client.put_object(
            Bucket=bucket, Key=image_key, Body=data, ContentType=content_type
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchBucket", "404"):
            client.create_bucket(Bucket=bucket)
            client.put_object(
                Bucket=bucket, Key=image_key, Body=data, ContentType=content_type
            )
        else:
            raise
