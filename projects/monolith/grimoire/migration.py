"""Copy Grimoire's data to a private R2 prefix for the factory migration.

The ``grimoire-migration-dump`` CronWorkflow
(``chart/templates/grimoire-migration-dump.yaml``) runs this handler after its
``pg-dump`` init container writes the dump files to
``GRIMOIRE_MIGRATION_WORK``. One run writes these objects under
``<prefix><run id>/`` in the Grimoire bucket:

- ``grimoire.dump``, ``vector.metadata``, ``source.inventory``: the files that
  the factory restore reads (``projects/grimoire/deploy/README.md``).
- ``objects/<key>``: a server-side copy of every bucket object outside
  ``<prefix>``. This includes every object that a database row references.
- ``missing-references.txt``: each referenced ``s3://`` location that the copy
  does not hold, one for each line.
- ``SHA256SUMS``: the SHA-256 of each object above, in ``sha256sum`` format.

The job reads each object back from R2 to calculate its digest, so the
manifest describes the stored bytes. It writes ``SHA256SUMS`` last. A run
without ``SHA256SUMS`` is incomplete, and the factory never reads it.
"""

from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from grimoire import images
from grimoire.jobs import DEFAULT_BUCKET
from grimoire.models import KnowledgeChunk, SessionEvent

logger = logging.getLogger("monolith.grimoire.migration")

DEFAULT_PREFIX = "migration/"
# The pg-dump init container writes these files (chart/files/grimoire-migration-dump.sh).
DUMP_FILES = ("grimoire.dump", "vector.metadata", "source.inventory")
OBJECTS = "objects/"
MISSING = "missing-references.txt"
MANIFEST = "SHA256SUMS"
# Below botocore's default pool of 10 connections for each client.
WORKERS = 8
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class RunSummary:
    run: str
    objects: int
    object_bytes: int
    missing: int


def run_id(now: datetime) -> str:
    """A UTC timestamp. Run ids sort in time order."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def check_prefix(prefix: str) -> str:
    # An empty prefix would copy earlier runs into each new run.
    if not prefix or prefix.startswith("/") or not prefix.endswith("/"):
        raise ValueError(f"migration prefix {prefix!r} must be a path that ends in /")
    return prefix


def check_key(key: str) -> str:
    # sha256sum escapes these characters, and the factory's readers expect the
    # plain form. Refuse the run instead of writing an ambiguous manifest line.
    if any(character in key for character in "\\\n\r"):
        raise ValueError(f"object key {key!r} cannot be written to {MANIFEST}")
    return key


def references(session: Session, bucket: str) -> set[str]:
    """Every object location that a Grimoire row references, as ``s3://`` URIs.

    Chunk images store a full URI. An uploaded handout stores a key in the
    handout bucket, which is ``bucket``. A handout that shows a chunk image
    references that chunk's ``image_ref``, so the first query covers it.
    """
    found: set[str] = set()
    image_refs = session.exec(
        select(KnowledgeChunk.image_ref).where(KnowledgeChunk.image_ref.is_not(None))
    )
    for image_ref in image_refs:
        # An unparsable ref stays in its stored form. The copy cannot hold it.
        parsed = images.parse_s3_uri(image_ref)
        found.add(f"s3://{parsed[0]}/{parsed[1]}" if parsed else image_ref)
    bodies = session.exec(
        select(SessionEvent.body).where(SessionEvent.kind == "handout")
    )
    for body in bodies:
        image = (body or {}).get("image")
        if (
            isinstance(image, dict)
            and image.get("source") == "upload"
            and isinstance(image.get("key"), str)
        ):
            found.add(f"s3://{bucket}/{image['key']}")
    return found


def list_keys(client, bucket: str, exclude_prefix: str) -> list[str]:
    """Every key in ``bucket`` outside ``exclude_prefix``, sorted."""
    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs = {"Bucket": bucket}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for item in page.get("Contents", []):
            if not item["Key"].startswith(exclude_prefix):
                keys.append(check_key(item["Key"]))
        if not page.get("IsTruncated"):
            return sorted(keys)
        token = page["NextContinuationToken"]


def stored_digest(client, bucket: str, key: str) -> tuple[int, str]:
    """The size and SHA-256 of the object that R2 stores at ``key``."""
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    digest = hashlib.sha256()
    size = 0
    try:
        for chunk in body.iter_chunks(chunk_size=CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    finally:
        body.close()
    return size, digest.hexdigest()


def manifest(sums: dict[str, str]) -> str:
    """``sha256sum`` lines, sorted by path. Paths are relative to the run."""
    return "".join(f"{digest}  {path}\n" for path, digest in sorted(sums.items()))


def write_run(
    client,
    bucket: str,
    prefix: str,
    work: Path,
    refs: set[str],
    now: datetime,
) -> RunSummary:
    """Write one complete run under ``<prefix><run id>/``."""
    run = f"{check_prefix(prefix)}{run_id(now)}/"
    sums: dict[str, str] = {}

    for name in DUMP_FILES:
        path = work / name
        with path.open("rb") as handle:
            local = hashlib.file_digest(handle, "sha256").hexdigest()
            handle.seek(0)
            # One PUT, as the hub's other R2 writes do. R2 refuses a single
            # PUT over about 5 GiB, which stops the run before SHA256SUMS.
            client.put_object(Bucket=bucket, Key=run + name, Body=handle)
        _, stored = stored_digest(client, bucket, run + name)
        if stored != local:
            raise RuntimeError(f"{name}: the stored object differs from the dump file")
        sums[name] = stored

    # List after the dump and the reference query. A row commits after its
    # object upload, so the listing holds every object that they reference.
    keys = list_keys(client, bucket, prefix)

    def copy(key: str) -> tuple[int, str]:
        target = run + OBJECTS + key
        client.copy_object(
            Bucket=bucket, Key=target, CopySource={"Bucket": bucket, "Key": key}
        )
        return stored_digest(client, bucket, target)

    object_bytes = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for key, (size, digest) in zip(keys, pool.map(copy, keys)):
            sums[OBJECTS + key] = digest
            object_bytes += size

    held = {f"s3://{bucket}/{key}" for key in keys}
    missing = sorted(refs - held)
    client.put_object(
        Bucket=bucket,
        Key=run + MISSING,
        Body="".join(f"{ref}\n" for ref in missing).encode(),
    )
    sums[MISSING] = stored_digest(client, bucket, run + MISSING)[1]

    client.put_object(Bucket=bucket, Key=run + MANIFEST, Body=manifest(sums).encode())
    return RunSummary(run, len(keys), object_bytes, len(missing))


async def grimoire_migration_dump(session: Session) -> None:
    """Upload the dump files and copy the bucket for the factory migration."""
    from grimoire.ingest import build_s3_client

    work = os.environ.get("GRIMOIRE_MIGRATION_WORK", "")
    if not work:
        raise RuntimeError("GRIMOIRE_MIGRATION_WORK unset; no dump files to upload")
    bucket = os.environ.get("GRIMOIRE_S3_BUCKET", DEFAULT_BUCKET)
    prefix = os.environ.get("GRIMOIRE_MIGRATION_PREFIX", DEFAULT_PREFIX)
    refs = references(session, bucket)
    # The copy takes minutes. Hold no transaction open during it.
    session.rollback()
    summary = write_run(
        build_s3_client(), bucket, prefix, Path(work), refs, datetime.now(UTC)
    )
    logger.info(
        "grimoire_migration_dump: wrote s3://%s/%s with %d objects (%d bytes); "
        "%d referenced objects missing",
        bucket,
        summary.run,
        summary.objects,
        summary.object_bytes,
        summary.missing,
    )
