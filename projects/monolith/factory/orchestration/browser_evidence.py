"""Pure browser evidence handoff checks, without storage or dispatch integration.

Guest JSON is untrusted. Expected identities and proof objects must come from
server observations, never fields copied from that JSON. Passing these checks
permits independent inspection; it does not constitute visual acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

MAX_HANDOFF_BYTES = 256 * 1024
MAX_INVENTORY_BYTES = 8 * 1024 * 1024


class EvidenceError(ValueError):
    """Missing, stale, malformed or mismatched evidence."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def _text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def _digest(value: object, length: int = 64) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None
    )


def _path(value: object) -> bool:
    return (
        _text(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in ("", ".", "..") for part in value.split("/"))
        and str(PurePosixPath(value)) == value
    )


def _object(value: object, fields: set[str]) -> dict:
    _require(
        type(value) is dict and set(value) == fields, "unexpected or missing fields"
    )
    return value


@dataclass(frozen=True)
class Provenance:
    repo: str
    base_sha: str
    head_sha: str
    target_sha: str
    harness_sha: str
    fixture_sha256: str
    checklist_sha256: str
    dependency_lock_sha256: str
    browser_name: str
    browser_version: str
    browser_binary_sha256: str

    def validate(self) -> None:
        _require(
            bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo)),
            "invalid repo",
        )
        for value in (self.base_sha, self.head_sha, self.target_sha, self.harness_sha):
            _require(_digest(value, 40), "expected exact git SHA")
        for value in (
            self.fixture_sha256,
            self.checklist_sha256,
            self.dependency_lock_sha256,
            self.browser_binary_sha256,
        ):
            _require(_digest(value), "expected exact provenance digest")
        _require(
            _text(self.browser_name) and _text(self.browser_version),
            "missing browser identity",
        )


@dataclass(frozen=True)
class Producer:
    run_id: str
    node_id: str
    attempt: int
    session_id: str
    turn: int
    lineage_id: str
    workload: str
    principal: str

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if name in ("attempt", "turn"):
                _require(type(value) is int and value > 0, "invalid attempt or turn")
            else:
                _require(_text(value), "missing producer identity")


@dataclass(frozen=True)
class Handoff:
    provenance: Provenance
    producer: Producer
    evidence_root: str
    manifest_path: str
    manifest_sha256: str
    inventory_sha256: str
    handoff_sha256: str


@dataclass(frozen=True)
class ExportProof:
    """Trusted completion observation, not an enqueue response or guest claim."""

    producer: Producer
    handoff_sha256: str
    state: str
    fingerprint: str


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def parse_handoff(
    raw: bytes, *, expected_provenance: Provenance, expected_producer: Producer
) -> Handoff:
    """Validate the declared artifact against the exact dispatch being awaited.

    The manifest path is relative to evidence_root, itself relative to the
    checkout. Filesystem consumers must also enforce realpath/no-symlink bounds.
    The inventory digest identifies the complete manifest-declared file list.
    """
    _require(
        type(raw) is bytes and 0 < len(raw) <= MAX_HANDOFF_BYTES,
        "missing or oversize handoff",
    )
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceError("invalid handoff JSON") from exc
    value = _object(
        value,
        {
            "version",
            "role",
            "status",
            "provenance",
            "producer",
            "evidence_root",
            "manifest_path",
            "manifest_sha256",
            "inventory_sha256",
        },
    )
    _require(
        type(value["version"]) is int and value["version"] == 1, "unsupported version"
    )
    _require(
        value["role"] == "capture-only" and value["status"] == "capture-complete",
        "capture incomplete or wrong role",
    )
    provenance = Provenance(
        **_object(value["provenance"], set(Provenance.__dataclass_fields__))
    )
    producer = Producer(
        **_object(value["producer"], set(Producer.__dataclass_fields__))
    )
    # Validate both sides: typed construction alone is not runtime validation.
    try:
        provenance.validate()
        expected_provenance.validate()
        producer.validate()
        expected_producer.validate()
    except TypeError as exc:
        raise EvidenceError("invalid typed identity") from exc
    _require(provenance == expected_provenance, "stale or mismatched provenance")
    _require(producer == expected_producer, "stale or mismatched producer")
    _require(
        _path(value["evidence_root"]) and _path(value["manifest_path"]),
        "unsafe evidence path",
    )
    _require(
        _digest(value["manifest_sha256"]) and _digest(value["inventory_sha256"]),
        "missing evidence digest",
    )
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return Handoff(
        provenance,
        producer,
        value["evidence_root"],
        value["manifest_path"],
        value["manifest_sha256"],
        value["inventory_sha256"],
        hashlib.sha256(canonical).hexdigest(),
    )


def validate_export(handoff: Handoff, proof: ExportProof) -> None:
    """Require the matching completed export before scheduling inspection."""
    _require(proof.producer == handoff.producer, "export producer mismatch")
    _require(proof.handoff_sha256 == handoff.handoff_sha256, "export handoff mismatch")
    _require(proof.state == "completed", "export not completed")
    _require(_digest(proof.fingerprint), "missing export fingerprint")


@dataclass(frozen=True)
class VerifiedInventory:
    """Byte verification only, not image completeness or visual acceptance."""

    manifest_sha256: str
    inventory_sha256: str
    files: int
    bytes: int


def _open_relative(directory: int, path: str) -> int:
    """Walk beneath an already-open root, rejecting symlinks at every step."""
    _require(_path(path), "unsafe inventory path")
    current = os.dup(directory)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
            )
            os.close(current)
            current = child
        return os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current
        )
    finally:
        os.close(current)


def verify_inventory(checkout: str, handoff: Handoff) -> VerifiedInventory:
    """Verify a restored inventory beneath a trusted checkout directory.

    Manifest format: {"files": [{"path": ..., "size": ..., "sha256": ...}]}.
    Paths are relative to evidence_root. inventory_sha256 hashes canonical JSON
    of that ordered files array (sorted keys, compact separators, ASCII escapes).
    The workspace manifest is separately bounded to 8 MiB; file reads stream with constant memory.
    Every declared file must be regular, present and match its size and digest.
    This does not evaluate screenshot geometry, checklist coverage or content.
    """
    checkout_fd = root_fd = None
    try:
        checkout_fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root_fd = _open_relative(checkout_fd, handoff.evidence_root)
        _require(
            stat.S_ISDIR(os.fstat(root_fd).st_mode), "evidence root is not directory"
        )
        with os.fdopen(_open_relative(root_fd, handoff.manifest_path), "rb") as stream:
            info = os.fstat(stream.fileno())
            _require(
                stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_INVENTORY_BYTES,
                "invalid manifest file",
            )
            raw = stream.read(MAX_INVENTORY_BYTES + 1)
        _require(
            len(raw) <= MAX_INVENTORY_BYTES
            and hashlib.sha256(raw).hexdigest() == handoff.manifest_sha256,
            "manifest digest mismatch",
        )
        manifest = _object(json.loads(raw, object_pairs_hook=_unique_object), {"files"})
        entries = manifest["files"]
        _require(type(entries) is list and bool(entries), "empty or invalid inventory")
        encoded = json.dumps(
            entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        _require(
            hashlib.sha256(encoded).hexdigest() == handoff.inventory_sha256,
            "inventory digest mismatch",
        )
        seen = set()
        total = 0
        for entry in entries:
            entry = _object(entry, {"path", "size", "sha256"})
            path, size = entry["path"], entry["size"]
            _require(_path(path), "unsafe inventory path")
            _require(
                path not in seen and path != handoff.manifest_path,
                "duplicate or self-referential inventory",
            )
            _require(
                type(size) is int and size >= 0 and _digest(entry["sha256"]),
                "invalid inventory metadata",
            )
            seen.add(path)
            with os.fdopen(_open_relative(root_fd, path), "rb") as stream:
                before = os.fstat(stream.fileno())
                _require(
                    stat.S_ISREG(before.st_mode) and before.st_size == size,
                    "file type or size mismatch",
                )
                digest, count = hashlib.sha256(), 0
                while chunk := stream.read(min(1024 * 1024, size - count + 1)):
                    count += len(chunk)
                    _require(count <= size, "file grew during verification")
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
            _require(
                count == size and digest.hexdigest() == entry["sha256"],
                "file digest mismatch",
            )
            _require(
                (before.st_mtime_ns, before.st_ctime_ns, before.st_size)
                == (after.st_mtime_ns, after.st_ctime_ns, after.st_size),
                "file changed during verification",
            )
            total += count
        return VerifiedInventory(
            handoff.manifest_sha256, handoff.inventory_sha256, len(entries), total
        )
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceError(f"inventory verification failed: {exc}") from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if checkout_fd is not None:
            os.close(checkout_fd)
