"""Trip image storage on Cloudflare R2 through its S3 API.

Trip photos are content-addressed (``img_<sha256[:12]>.jpg``) and uploaded to the
``monolith-trips`` bucket. The client comes from core.s3 (shared chart
credentials, path-style addressing, endpoint scheme guard) and the
idempotent put + auto-create-bucket fallback mirrors chat.store._blob_s3_put.

Production uses a pre-existing bucket. The historical auto-create fallback
remains, but R2 operators must provision buckets separately with authorization.
The dummy credential defaults are retained for legacy/tests, not R2 access.
"""

import logging
import os

from core.s3 import path_style_client

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
    from botocore.exceptions import ClientError

    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if not endpoint:
        raise RuntimeError("SEAWEEDFS_S3_ENDPOINT unset; cannot store trip image")
    client = path_style_client(endpoint)

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
