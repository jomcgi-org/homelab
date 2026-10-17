from __future__ import annotations

from collections.abc import Iterator

IMAGE_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


def parse_s3_uri(uri: str) -> tuple[str, str] | None:
    if not uri.startswith("s3://"):
        return None
    bucket, _, key = uri[len("s3://") :].partition("/")
    return (bucket, key) if bucket and key else None


def content_type(key: str) -> str:
    suffix = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return IMAGE_CONTENT_TYPES.get(suffix, "application/octet-stream")


def iter_body(body, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    try:
        yield from body.iter_chunks(chunk_size=chunk_size)
    finally:
        body.close()
