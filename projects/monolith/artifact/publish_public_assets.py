"""Append-only public asset publisher, run only by the trusted release Job."""

from __future__ import annotations

import argparse
import json
import logging
import stat
from pathlib import Path

from artifact.public_assets import (
    MAX_ENTRY_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    PREFIX,
    _client,
    canonical,
    digest,
    entry_key,
    missing,
    object_key,
    read_object,
    valid_path,
    validate_entry,
)
from artifact.s3 import _bucket

logger = logging.getLogger("monolith.public_assets.publish")


def require_retention(client, bucket: str) -> dict[str, str]:
    """Reject unsafe rules; record configuration-only permission limits."""
    try:
        config = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code == "NoSuchLifecycleConfiguration":
            return {"status": "verified", "reason": "no-lifecycle-configuration"}
        if code == "AccessDenied":
            logger.warning(
                "lifecycle-unverified: object writer cannot inspect bucket configuration; "
                "no retention duration is established (owner follow-up #7960)"
            )
            return {"status": "unverified", "reason": "configuration-access-denied"}
        raise RuntimeError("cannot establish archive lifecycle policy") from exc
    rules = config.get("Rules")
    if not isinstance(rules, list):
        raise TypeError("unknown archive lifecycle configuration")
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("Status") not in {
            "Enabled",
            "Disabled",
        }:
            raise ValueError("unknown archive lifecycle rule")
        if rule["Status"] == "Disabled":
            continue
        filt = rule.get("Filter", {})
        if not isinstance(filt, dict):
            raise TypeError("unknown archive lifecycle filter")
        conjunction = filt.get("And", {})
        if not isinstance(conjunction, dict):
            raise TypeError("unknown archive lifecycle conjunction")
        prefix = rule.get("Prefix", filt.get("Prefix", conjunction.get("Prefix", "")))
        if not isinstance(prefix, str):
            raise TypeError("unknown archive lifecycle prefix")
        applies = PREFIX.startswith(prefix) or prefix.startswith(PREFIX)
        if applies and ("Expiration" in rule or rule.get("Transitions")):
            raise ValueError("bucket lifecycle may expire archived frontend assets")
    return {"status": "verified", "reason": "no-matching-expiration"}


def inventory(root: Path) -> tuple[dict[str, dict], dict[str, bytes]]:
    """Admit only generated immutable originals and existing br/gzip siblings."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("immutable source is not a real directory")
    entries, objects = {}, {}
    total = files = 0
    for source in sorted(root.rglob("*")):
        mode = source.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError("immutable source contains a link or special file")
        if stat.S_ISDIR(mode):
            continue
        path = source.relative_to(root).as_posix()
        encoding = "identity"
        if path.endswith(".br"):
            path, encoding = path[:-3], "br"
        elif path.endswith(".gz"):
            path, encoding = path[:-3], "gzip"
        if not valid_path(path):
            raise ValueError("source path is outside public immutable assets")
        size = source.stat().st_size
        files += 1
        total += size
        if files > MAX_FILES or size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("immutable source exceeds publication bounds")
        body = source.read_bytes()
        if len(body) != size:
            raise ValueError("immutable source changed while reading")
        sha = digest(body)
        objects[sha] = body
        entry = entries.setdefault(path, {"version": 1, "path": path, "variants": {}})
        entry["variants"][encoding] = {"sha256": sha, "size": size}
    if not entries or any("identity" not in e["variants"] for e in entries.values()):
        raise ValueError("immutable source is empty or has an orphan compressed file")
    return entries, objects


def append_verified(
    client, bucket: str, key: str, body: bytes, content_type: str
) -> None:
    """Never replace a key; conditional create makes concurrent publishers safe."""
    try:
        existing = read_object(client, bucket, key, max(len(body), 1))
    except Exception as exc:
        if not missing(exc):
            raise
    else:
        if existing != body:
            raise ValueError("immutable archive key collision")
        return
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            Metadata={"sha256": digest(body)},
            IfNoneMatch="*",
        )
    except Exception as exc:
        # A competing create is safe only when the winning bytes match.
        if getattr(exc, "response", {}).get("Error", {}).get("Code") not in {
            "PreconditionFailed",
            "412",
            "ConditionalRequestConflict",
            "409",
        }:
            raise
    observed = read_object(client, bucket, key, max(len(body), 1))
    if observed != body or digest(observed) != digest(body):
        raise ValueError("immutable archive readback verification failed")


def retained_entry(client, bucket: str, path: str, identity: bytes) -> dict | None:
    """Keep a sealed path's verified bytes, independent of compression settings."""
    try:
        raw = read_object(client, bucket, entry_key(path), MAX_ENTRY_BYTES)
    except Exception as exc:
        if missing(exc):
            return None
        raise
    entry = json.loads(raw)
    variants = validate_entry(entry, path)
    if variants["identity"] != {"sha256": digest(identity), "size": len(identity)}:
        raise ValueError("immutable archive identity collision")
    for encoding, item in variants.items():
        body = read_object(client, bucket, object_key(item["sha256"]), MAX_FILE_BYTES)
        if len(body) != item["size"] or digest(body) != item["sha256"]:
            raise ValueError("sealed immutable representation failed verification")
        if encoding == "identity" and body != identity:
            raise ValueError("immutable archive identity collision")
    return entry


def seal_entry(client, bucket: str, path: str, entry: dict, identity: bytes) -> dict:
    """Accept a concurrent winner only after verifying its complete inventory."""
    body = canonical(entry)
    try:
        client.put_object(
            Bucket=bucket,
            Key=entry_key(path),
            Body=body,
            ContentType="application/json",
            Metadata={"sha256": digest(body)},
            IfNoneMatch="*",
        )
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") not in {
            "PreconditionFailed",
            "412",
            "ConditionalRequestConflict",
            "409",
        }:
            raise
    observed = retained_entry(client, bucket, path, identity)
    if observed is None:
        raise ValueError("immutable archive manifest readback missing")
    return observed


def publish(root: Path, *, client=None, bucket: str | None = None) -> str:
    client = _client() if client is None else client
    bucket = _bucket() if bucket is None else bucket
    lifecycle = require_retention(client, bucket)
    entries, objects = inventory(root)
    pending = {}
    # Check sealed paths before any upload: a candidate must not repair a missing
    # or corrupt old object and silently turn an invalid archive into a success.
    for path, entry in sorted(entries.items()):
        identity = objects[entry["variants"]["identity"]["sha256"]]
        observed = retained_entry(client, bucket, path, identity)
        if observed is None:
            pending[path] = entry
        else:
            entries[path] = observed
    needed = {
        item["sha256"]
        for entry in pending.values()
        for item in entry["variants"].values()
    }
    # Verify all new representations before exposing any new per-path manifest.
    for sha in sorted(needed):
        append_verified(
            client, bucket, object_key(sha), objects[sha], "application/octet-stream"
        )
    for path, entry in sorted(pending.items()):
        entries[path] = seal_entry(
            client,
            bucket,
            path,
            entry,
            objects[entry["variants"]["identity"]["sha256"]],
        )
    # The Job succeeds only after this exact complete inventory is persisted.
    # Per-path entries are independently sealed; partial builds cannot point a
    # reader at a representation that has not already passed readback checks.
    manifest = canonical({"version": 1, "entries": entries, "lifecycle": lifecycle})
    build_id = digest(manifest)
    append_verified(
        client,
        bucket,
        PREFIX + "builds/" + build_id + ".json",
        manifest,
        "application/json",
    )
    logger.info(
        "verified public immutable build %s (%d paths, %d objects)",
        build_id,
        len(entries),
        len(
            {
                item["sha256"]
                for entry in entries.values()
                for item in entry["variants"].values()
            }
        ),
    )
    return build_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(publish(args.directory))


if __name__ == "__main__":
    main()
