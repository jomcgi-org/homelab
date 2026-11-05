"""Upload with the existing one-shot missing-bucket fallback."""


def put_object(
    client, *, bucket: str, key: str, body: bytes, content_type: str
) -> None:
    """Create a missing bucket and repeat the identical PUT once."""
    from botocore.exceptions import ClientError

    request = dict(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    try:
        client.put_object(**request)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code", "") not in ("NoSuchBucket", "404"):
            raise
        client.create_bucket(Bucket=bucket)
        client.put_object(**request)
