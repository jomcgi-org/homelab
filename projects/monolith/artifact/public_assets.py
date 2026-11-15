"""Read-only access to verified public frontend immutable files.

This namespace is separate from untrusted agent HTML artifacts. Only the trusted
release publisher creates its manifests; callers cannot choose a bucket or key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from functools import lru_cache

from fastapi import APIRouter, Request, Response

from artifact.s3 import _bucket

PREFIX = "public-frontend/immutable/"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 4096
MAX_ENTRY_BYTES = 16384
CACHE_CONTROL = "public, max-age=31536000, immutable"
TYPES = {
    "js": "text/javascript",
    "css": "text/css",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "avif": "image/avif",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "ico": "image/x-icon",
}
_PATH = re.compile(
    r"(assets|chunks|entry|nodes|workers)/([A-Za-z0-9_.-]+)\.([a-z0-9]+)\Z"
)
_HASH = re.compile(r"[A-Za-z0-9_-]{8}\Z")
_HEX = re.compile(r"[a-f0-9]{64}\Z")
logger = logging.getLogger("monolith.public_assets")
router = APIRouter(prefix="/internal/public-assets", tags=["public-assets"])


@lru_cache(maxsize=1)
def _client():
    """Use the pod's existing identity, with bounded archive I/O and no retries."""
    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if not endpoint.startswith("https://"):
        raise RuntimeError("public archive requires the configured HTTPS S3 endpoint")
    if not os.environ.get("S3_ACCESS_KEY_ID") or not os.environ.get(
        "S3_SECRET_ACCESS_KEY"
    ):
        raise RuntimeError("public archive S3 identity is not configured")
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,  # nosemgrep: boto3-endpoint-url-missing-scheme
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_REGION", "auto"),
        config=Config(
            s3={"addressing_style": "path"},
            connect_timeout=1,
            read_timeout=2,
            retries={"total_max_attempts": 1},
        ),
    )


def valid_path(path: str) -> bool:
    if not isinstance(path, str) or len(path) > 240:
        return False
    match = _PATH.fullmatch(path)
    if match is None or match[3] not in TYPES or ".." in path:
        return False
    stem = match[2]
    return bool(
        _HASH.fullmatch(stem)
        or (len(stem) > 9 and stem[-9] in ".-" and _HASH.fullmatch(stem[-8:]))
    )


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def entry_key(path: str) -> str:
    if not valid_path(path):
        raise ValueError("invalid immutable asset path")
    return PREFIX + "entries/" + digest(path.encode()) + ".json"


def object_key(sha: str) -> str:
    if not isinstance(sha, str) or not _HEX.fullmatch(sha):
        raise ValueError("invalid immutable asset digest")
    return PREFIX + "objects/" + sha


def missing(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}


def read_object(client, bucket: str, key: str, limit: int) -> bytes:
    response = client.get_object(Bucket=bucket, Key=key)
    stream = response["Body"]
    try:
        if response.get("ContentLength", limit + 1) > limit:
            raise ValueError("archived object exceeds limit")
        body = stream.read(limit + 1)
        if len(body) > limit or len(body) != response["ContentLength"]:
            raise ValueError("archived object size mismatch")
        return body
    finally:
        stream.close()


def validate_entry(entry: object, path: str) -> dict:
    if (
        not isinstance(entry, dict)
        or set(entry) != {"version", "path", "variants"}
        or entry["version"] != 1
        or entry["path"] != path
    ):
        raise ValueError("invalid archived asset manifest")
    variants = entry["variants"]
    if (
        not isinstance(variants, dict)
        or "identity" not in variants
        or not set(variants) <= {"identity", "br", "gzip"}
    ):
        raise ValueError("invalid archived asset representations")
    for item in variants.values():
        if not isinstance(item, dict) or set(item) != {"sha256", "size"}:
            raise ValueError("invalid archived asset representation")
        object_key(item["sha256"])
        if type(item["size"]) is not int or not 0 <= item["size"] <= MAX_FILE_BYTES:
            raise ValueError("invalid archived asset size")
    return variants


def _not_modified(header: str, etag: str) -> bool:
    return any(
        value.strip().removeprefix("W/") in {"*", etag} for value in header.split(",")
    )


@router.head("/{asset_path:path}", include_in_schema=False)
@router.get("/{asset_path:path}")
def get_public_asset(asset_path: str, request: Request) -> Response:
    def failure(status: int) -> Response:
        return Response(status_code=status, headers={"Cache-Control": "no-store"})

    if (
        not valid_path(asset_path)
        or b"%" in request.scope.get("raw_path", b"")
        or set(request.query_params) - {"encoding"}
        or len(request.query_params.getlist("encoding")) > 1
    ):
        return failure(404)
    encoding = request.query_params.get("encoding", "identity")
    if encoding not in {"identity", "br", "gzip"}:
        return failure(404)
    try:
        client, bucket = _client(), _bucket()
        try:
            raw = read_object(client, bucket, entry_key(asset_path), MAX_ENTRY_BYTES)
        except Exception as exc:
            if missing(exc):
                return failure(404)
            raise
        variants = validate_entry(json.loads(raw), asset_path)
        if encoding not in variants:
            return failure(406)
        item = variants[encoding]
        body = read_object(client, bucket, object_key(item["sha256"]), MAX_FILE_BYTES)
        if len(body) != item["size"] or digest(body) != item["sha256"]:
            raise ValueError("archived asset failed integrity verification")
        headers = {
            "Cache-Control": CACHE_CONTROL,
            "Content-Type": TYPES[asset_path.rsplit(".", 1)[1]],
            "Content-Length": str(len(body)),
            "ETag": f'"{item["sha256"]}"',
            "Vary": "Accept-Encoding",
            "X-Content-Type-Options": "nosniff",
        }
        if encoding != "identity":
            headers["Content-Encoding"] = encoding
        if _not_modified(request.headers.get("if-none-match", ""), headers["ETag"]):
            headers.pop("Content-Length")
            return Response(status_code=304, headers=headers)
        return Response(
            content=b"" if request.method == "HEAD" else body, headers=headers
        )
    except Exception:
        logger.exception("public immutable asset unavailable")
        return failure(503)
