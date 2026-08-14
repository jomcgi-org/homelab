"""Measure content-defined chunk reuse across flattened rootfs images.

This is the ADR embervm/028 Phase 0 sizing harness. It intentionally has no OCI,
EROFS, encryption, or object-store dependencies: callers produce candidate
flattened images separately, then this tool measures how deduplication scope
changes their physical byte footprint.

The chunker is a deterministic FastCDC-class measurement implementation. Its
parameters and algorithm id are emitted in every report. It does not freeze the
production manifest format or cryptographic construction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Mapping, Sequence

_MASK64 = (1 << 64) - 1
_GEAR_ALGORITHM = "gear-v1"
_FIXED_ALGORITHM = "fixed-v1"
_ALGORITHMS = (_GEAR_ALGORITHM, _FIXED_ALGORITHM)


def _splitmix64(value: int) -> int:
    """Return one stable pseudo-random 64-bit value for the gear table."""

    value = (value + 0x9E3779B97F4A7C15) & _MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK64
    return value ^ (value >> 31)


_GEAR = tuple(_splitmix64(index) for index in range(256))


@dataclass(frozen=True)
class ChunkingParameters:
    minimum: int = 256 * 1024
    average: int = 512 * 1024
    maximum: int = 2 * 1024 * 1024
    algorithm: str = _GEAR_ALGORITHM

    def validate(self) -> None:
        if self.minimum <= 0:
            raise ValueError("minimum chunk size must be positive")
        if self.minimum > self.average or self.average > self.maximum:
            raise ValueError("chunk sizes must satisfy minimum <= average <= maximum")
        if self.average & (self.average - 1):
            raise ValueError("average chunk size must be a power of two")
        if self.algorithm not in _ALGORITHMS:
            raise ValueError(
                "chunk algorithm must be one of: " + ", ".join(_ALGORITHMS)
            )

    @property
    def early_mask(self) -> int:
        bits = self.average.bit_length() - 1
        return (1 << (bits + 1)) - 1

    @property
    def late_mask(self) -> int:
        bits = self.average.bit_length() - 1
        return (1 << max(bits - 1, 1)) - 1


@dataclass(frozen=True)
class ImageSpec:
    name: str
    account: str
    principal: str
    path: Path


@dataclass(frozen=True)
class ChunkRecord:
    digest: str
    size: int


def _cut(buffer: bytearray, params: ChunkingParameters) -> int | None:
    """Find the first content-defined boundary in buffer, if one is ready."""

    if len(buffer) < params.minimum:
        return None

    fingerprint = 0
    normal_end = min(len(buffer), params.average)
    for index in range(params.minimum, normal_end):
        fingerprint = ((fingerprint << 1) + _GEAR[buffer[index]]) & _MASK64
        if fingerprint & params.early_mask == 0:
            return index + 1

    maximum_end = min(len(buffer), params.maximum)
    for index in range(normal_end, maximum_end):
        fingerprint = ((fingerprint << 1) + _GEAR[buffer[index]]) & _MASK64
        if fingerprint & params.late_mask == 0:
            return index + 1

    if len(buffer) >= params.maximum:
        return params.maximum
    return None


def iter_chunk_records(
    stream: BinaryIO,
    params: ChunkingParameters,
    read_size: int = 1024 * 1024,
) -> Iterator[ChunkRecord]:
    """Yield content digests and sizes without retaining the whole image."""

    params.validate()
    if read_size <= 0:
        raise ValueError("read_size must be positive")

    if params.algorithm == _FIXED_ALGORITHM:
        buffer = bytearray()
        reached_eof = False
        while not reached_eof:
            block = stream.read(read_size)
            if block:
                buffer.extend(block)
            else:
                reached_eof = True
            while len(buffer) >= params.average or (reached_eof and buffer):
                boundary = min(len(buffer), params.average)
                chunk = bytes(buffer[:boundary])
                del buffer[:boundary]
                yield ChunkRecord(hashlib.sha256(chunk).hexdigest(), len(chunk))
        return

    buffer = bytearray()
    reached_eof = False
    while not reached_eof:
        block = stream.read(read_size)
        if block:
            buffer.extend(block)
        else:
            reached_eof = True

        while buffer:
            boundary = _cut(buffer, params)
            if boundary is None:
                if not reached_eof:
                    break
                boundary = len(buffer)
            chunk = bytes(buffer[:boundary])
            del buffer[:boundary]
            yield ChunkRecord(hashlib.sha256(chunk).hexdigest(), len(chunk))


def _parse_image(raw: Mapping[str, object], base_dir: Path) -> ImageSpec:
    required = ("name", "account", "principal", "path")
    missing = [
        key for key in required if not isinstance(raw.get(key), str) or not raw[key]
    ]
    if missing:
        raise ValueError(
            "image fields must be non-empty strings: " + ", ".join(missing)
        )
    path = Path(str(raw["path"]))
    if not path.is_absolute():
        path = base_dir / path
    return ImageSpec(
        name=str(raw["name"]),
        account=str(raw["account"]),
        principal=str(raw["principal"]),
        path=path,
    )


def load_specs(path: Path) -> list[ImageSpec]:
    """Load and validate a versioned sizing-manifest document."""

    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or raw.get("format_version") != 1:
        raise ValueError("input format_version must be 1")
    images = raw.get("images")
    if not isinstance(images, list) or not images:
        raise ValueError("input images must be a non-empty list")
    if any(not isinstance(image, dict) for image in images):
        raise ValueError("every image entry must be an object")
    specs = [_parse_image(image, path.parent) for image in images]
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ValueError("image names must be unique")
    missing_paths = [str(spec.path) for spec in specs if not spec.path.is_file()]
    if missing_paths:
        raise ValueError("image paths do not exist: " + ", ".join(missing_paths))
    return specs


def _scope_summary(
    logical_bytes: int, chunks: Mapping[object, int]
) -> dict[str, object]:
    stored_bytes = sum(chunks.values())
    return _scope_summary_values(logical_bytes, stored_bytes, len(chunks))


def _scope_summary_values(
    logical_bytes: int, stored_bytes: int, chunks: int
) -> dict[str, object]:
    ratio = logical_bytes / stored_bytes if stored_bytes else 1.0
    return {
        "chunks": chunks,
        "stored_bytes": stored_bytes,
        "saved_bytes": logical_bytes - stored_bytes,
        "dedup_ratio": round(ratio, 6),
    }


def measure(
    specs: Iterable[ImageSpec],
    params: ChunkingParameters,
    read_size: int = 1024 * 1024,
) -> dict[str, object]:
    """Measure aggregate storage under per-image, principal, account, and global scopes."""

    params.validate()
    seen_global: dict[str, int] = {}
    seen_account: dict[tuple[str, str], int] = {}
    seen_principal: dict[tuple[str, str, str], int] = {}
    image_results: list[dict[str, object]] = []
    logical_bytes = 0
    total_chunks = 0

    for spec in specs:
        image_bytes = 0
        image_chunks = 0
        image_unique: dict[str, int] = {}
        new_global = 0
        new_account = 0
        new_principal = 0

        with spec.path.open("rb") as stream:
            for chunk in iter_chunk_records(stream, params, read_size):
                image_bytes += chunk.size
                image_chunks += 1
                image_unique.setdefault(chunk.digest, chunk.size)

                if chunk.digest not in seen_global:
                    seen_global[chunk.digest] = chunk.size
                    new_global += chunk.size
                account_key = (spec.account, chunk.digest)
                if account_key not in seen_account:
                    seen_account[account_key] = chunk.size
                    new_account += chunk.size
                principal_key = (spec.account, spec.principal, chunk.digest)
                if principal_key not in seen_principal:
                    seen_principal[principal_key] = chunk.size
                    new_principal += chunk.size

        logical_bytes += image_bytes
        total_chunks += image_chunks
        image_results.append(
            {
                "name": spec.name,
                "account": spec.account,
                "principal": spec.principal,
                "path": str(spec.path),
                "logical_bytes": image_bytes,
                "chunks": image_chunks,
                "unique_chunks": len(image_unique),
                "new_bytes": {
                    "global": new_global,
                    "account": new_account,
                    "principal": new_principal,
                },
            }
        )

    return {
        "report_version": 1,
        "algorithm": {
            "id": params.algorithm,
            "minimum_bytes": params.minimum,
            "average_bytes": params.average,
            "maximum_bytes": params.maximum,
        },
        "logical_bytes": logical_bytes,
        "images": image_results,
        "scopes": {
            "per_image": _scope_summary_values(
                logical_bytes, logical_bytes, total_chunks
            ),
            "principal": _scope_summary(logical_bytes, seen_principal),
            "account": _scope_summary(logical_bytes, seen_account),
            "global": _scope_summary(logical_bytes, seen_global),
        },
        "notes": [
            "Manifest, encryption metadata, and filesystem allocation overhead are excluded.",
            "Per-image new_bytes attribution follows input order; aggregate scope totals do not.",
            "The measurement chunker does not freeze the production manifest format.",
        ],
    }


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="versioned JSON image manifest")
    parser.add_argument("--minimum", type=_positive_int, default=256 * 1024)
    parser.add_argument("--average", type=_positive_int, default=512 * 1024)
    parser.add_argument("--maximum", type=_positive_int, default=2 * 1024 * 1024)
    parser.add_argument("--read-size", type=_positive_int, default=1024 * 1024)
    parser.add_argument(
        "--algorithm",
        choices=_ALGORITHMS,
        default=_GEAR_ALGORITHM,
        help="gear-v1 for content-defined chunks, fixed-v1 for the baseline",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        params = ChunkingParameters(
            args.minimum, args.average, args.maximum, args.algorithm
        )
        report = measure(load_specs(args.manifest), params, args.read_size)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"measure_chunks: {exc}") from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
