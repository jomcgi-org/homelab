import hashlib
from pathlib import Path

HARNESS_VERSION = "0.1.0"


def cell_key(
    *,
    prompt: str,
    fixture_hash: str,
    verifier_repr: str,
    model_id: str,
    params_repr: str,
) -> str:
    h = hashlib.sha256()
    for part in (
        prompt,
        fixture_hash,
        verifier_repr,
        HARNESS_VERSION,
        model_id,
        params_repr,
    ):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")  # domain separator so concatenation is unambiguous
    return h.hexdigest()[:12]


def fixture_hash(fixture_dir: Path) -> str:
    """Hash of the frozen fixture tree: sorted (relpath, bytes) so it is deterministic."""
    h = hashlib.sha256()
    for f in sorted(p for p in fixture_dir.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(fixture_dir)).encode())
        h.update(b"\x00")
        h.update(f.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()[:12]


def _slug(model_id: str) -> str:
    return model_id.replace("/", "__")


def cell_path(results_root: Path, model_id: str, task_id: str, key: str) -> Path:
    return results_root / _slug(model_id) / task_id / f"{key}.json"


def is_cached(path: Path) -> bool:
    return path.exists()
