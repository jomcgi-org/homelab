"""Copy Grimoire's data to a private R2 prefix for the factory migration.

The ``grimoire-migration-dump`` CronWorkflow
(``chart/templates/grimoire-migration-dump.yaml``) runs this handler after its
``pg-dump`` init container writes the dump files and ``references.csv`` to
``GRIMOIRE_MIGRATION_WORK``. The init container reads all of them in one
snapshot, and this handler reads no database row. One run writes these objects
under ``<prefix><run id>/`` in the Grimoire bucket:

- ``grimoire.dump``, ``vector.metadata``, ``source.inventory``: the files that
  the factory restore reads (``projects/grimoire/deploy/README.md``).
- ``objects/<key>``: a server-side copy of every bucket object outside
  ``<prefix>``. This includes every object that a database row references.
- ``missing-references.txt``: each ``s3://`` location that a row in the
  snapshot references and the copy does not hold, one for each line.
- ``SHA256SUMS``: the SHA-256 of each object above, in ``sha256sum`` format.

The job reads each object back from R2 to calculate its digest, so the
manifest describes the stored bytes. Each stored object must match its
source: the local dump file, or the bucket object that it copies. R2 gives no
SHA-256 for an object, and a multipart ETag is not an MD5, so the job reads
each source object too. A difference stops the run. It writes ``SHA256SUMS``
last. A run without ``SHA256SUMS`` is incomplete, and the factory never reads
it.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

from grimoire import images
from grimoire.jobs import DEFAULT_BUCKET

logger = logging.getLogger("monolith.grimoire.migration")

DEFAULT_PREFIX = "migration/"
# The pg-dump init container writes these files (chart/files/grimoire-migration-dump.sh).
DUMP_FILES = ("grimoire.dump", "vector.metadata", "source.inventory")
# Also from the init container, in the dump's snapshot. The run does not hold it.
REFERENCES = "references.csv"
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


def references(path: Path, bucket: str) -> set[str]:
    """Every object location in ``path``, as ``s3://`` URIs.

    The init container writes ``path`` with
    ``chart/files/grimoire-references.sql``, in the dump's snapshot. Each CSV
    row is ``image_ref,<stored value>`` for a chunk image, which stores a full
    URI, or ``upload,<key>`` for an uploaded handout, which stores a key in the
    handout bucket, ``bucket``.
    """
    found: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.reader(handle):
            match row:
                case ["image_ref", image_ref]:
                    # An unparsable ref stays in its stored form. The copy
                    # cannot hold it.
                    parsed = images.parse_s3_uri(image_ref)
                    found.add(f"s3://{parsed[0]}/{parsed[1]}" if parsed else image_ref)
                case ["upload", key]:
                    found.add(f"s3://{bucket}/{key}")
                case _:
                    raise ValueError(f"{path.name}: unknown reference row {row!r}")
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

    # List after the snapshot. A row commits after its object upload, so the
    # listing holds every object that a snapshot row references.
    keys = list_keys(client, bucket, prefix)

    def copy(key: str) -> tuple[int, str]:
        target = run + OBJECTS + key
        source = stored_digest(client, bucket, key)
        client.copy_object(
            Bucket=bucket, Key=target, CopySource={"Bucket": bucket, "Key": key}
        )
        stored = stored_digest(client, bucket, target)
        # A source that changes after its read also stops the run.
        if stored != source:
            raise RuntimeError(f"{OBJECTS}{key}: the copy differs from its source")
        return stored

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
    # jobs_main gives each job a session. This job uses none: a row that
    # changes after the snapshot must not change the run.
    refs = references(Path(work) / REFERENCES, bucket)
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
