"""Prepare a capture declaration; server completion binds the producer identity.

Existing before/after bundles are data. The sibling handoff checker reuses the
sheet harness to validate them. This helper never reviews pixels or dispatches
an agent, and never emits a claim about an Ember identity or export completion.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys

INPUT_KINDS = ("fixture", "checklist", "dependency_lock", "browser_binary")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def safe_relative(value):
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError("expected a safe checkout-relative path")
    return Path(value)


def regular(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"not a regular evidence file: {path}")


def sha256(path):
    regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def prepare(args):
    checkout = Path(args.checkout).absolute()
    if checkout.resolve() != checkout:
        raise ValueError("checkout must not traverse symlinks")
    root = checkout / safe_relative(args.evidence_root)
    output = checkout / safe_relative(args.output)
    if root.resolve() != root or not root.is_dir():
        raise ValueError("evidence root must be an existing real directory")
    if output.is_relative_to(root) or output.resolve() != output:
        raise ValueError(
            "handoff output must be outside evidence root without symlinks"
        )
    if output.exists():
        raise ValueError("refusing stale handoff output")
    checker = Path(__file__).with_name("browser_handoff_check.py")
    harness = Path(__file__).with_name("sheet_check.py")
    if not checker.is_file() or not harness.is_file():
        raise ValueError(
            "missing corrected sheet_check.py or browser_handoff_check.py beside helper"
        )
    # Reject special files and links before copying inputs or hashing a tree.
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f"unsafe evidence entry: {path}")
    for side in ("before", "after"):
        regular(root / side / "results.json")
    source_provenance = root / "provenance.json"
    regular(source_provenance)
    captured_provenance = json.loads(source_provenance.read_bytes())
    for name in ("base_sha", "target_sha", "head_sha", "harness_sha"):
        if captured_provenance.get(name) != getattr(args, name):
            raise ValueError("stale comparison provenance: " + name)
    metadata = [root / name for name in ("inputs.json", "manifest.json")]
    input_root = root / "inputs"
    if input_root.exists() or any(path.exists() for path in metadata):
        raise ValueError("refusing stale preparation metadata")
    provenance = {
        name: getattr(args, name)
        for name in (
            "repo",
            "base_sha",
            "head_sha",
            "target_sha",
            "harness_sha",
            "browser_version",
        )
    }
    provenance["browser_name"] = "chromium"
    created = []
    try:
        input_root.mkdir()
        inputs = {}
        for kind in INPUT_KINDS:
            source = Path(getattr(args, kind))
            if source.resolve() != source.absolute():
                raise ValueError(f"capture input must not traverse symlinks: {source}")
            regular(source)
            target = input_root / kind
            with source.open("rb") as reader, target.open("xb") as writer:
                created.append(target)
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            inputs[kind] = target.relative_to(root).as_posix()
            provenance[kind + "_sha256"] = sha256(target)
            if sha256(source) != provenance[kind + "_sha256"]:
                raise ValueError(f"capture input changed while copying: {kind}")
        with metadata[0].open("xb") as stream:
            created.append(metadata[0])
            stream.write(encoded(inputs))
        entries = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"symlink appeared in evidence: {path}")
            if path.is_dir():
                continue
            regular(path)
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
        manifest = encoded({"files": entries})
        if len(manifest) > 8 * 1024 * 1024:
            raise ValueError("workspace inventory exceeds 8 MiB")
        with metadata[1].open("xb") as stream:
            created.append(metadata[1])
            stream.write(manifest)
        declaration = {
            "version": 1,
            "role": "capture-only",
            "status": "capture-complete",
            "provenance": provenance,
            "evidence_root": args.evidence_root,
            "manifest_path": "manifest.json",
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "inventory_sha256": hashlib.sha256(encoded(entries)).hexdigest(),
        }
        # Structural validation needs a Producer. This sentinel is internal to
        # this process and never emitted or presented as an observed identity.
        local_validation = declaration | {
            "producer": {
                "run_id": "unbound-local-validation",
                "node_id": "unbound-local-validation",
                "attempt": 1,
                "session_id": "unbound-local-validation",
                "turn": 1,
                "lineage_id": "unbound-local-validation",
                "workload": "unbound-local-validation",
                "principal": "unbound-local-validation",
            }
        }
        checked = subprocess.run(
            [sys.executable, "-I", "-B", str(checker)],
            input=encoded({"checkout": str(checkout), "handoff": local_validation}),
            capture_output=True,
            timeout=300,
        )
        if checked.returncode:
            detail = (checked.stdout + checked.stderr).decode("utf-8", "replace")[
                -2000:
            ]
            raise ValueError(
                "capture integrity check failed (including required dependencies): "
                + detail
            )
        verdict = json.loads(checked.stdout)
        if verdict.get("status") != "ok":
            raise ValueError("capture checker did not verify the bundle")
        expected_verdict = {
            "handoff_sha256": hashlib.sha256(encoded(local_validation)).hexdigest(),
            "manifest_sha256": declaration["manifest_sha256"],
            "inventory_sha256": declaration["inventory_sha256"],
            **{
                name: provenance[name]
                for name in ("base_sha", "head_sha", "harness_sha")
            },
            "files": len(entries),
            "bytes": sum(entry["size"] for entry in entries),
        }
        if any(verdict.get(name) != value for name, value in expected_verdict.items()):
            raise ValueError("capture checker returned mismatched verification")
        raw = encoded(declaration)
        if len(raw) > 256 * 1024:
            raise ValueError("capture declaration exceeds turn artifact limit")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            created.append(output)
            stream.write(raw)
        return {
            "status": "capture-complete",
            "output": args.output,
            "files": len(entries),
            "bytes": sum(entry["size"] for entry in entries),
        }
    except Exception:
        # Only remove files created here. Existing browser artifacts survive a
        # missing dependency, clipped image or stale SHA, ready for diagnosis.
        for path in reversed(created):
            path.unlink(missing_ok=True)
        if input_root.exists():
            input_root.rmdir()
        raise


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    for name in (
        "checkout",
        "evidence_root",
        "repo",
        "base_sha",
        "head_sha",
        "target_sha",
        "harness_sha",
        "browser_version",
        *INPUT_KINDS,
        "output",
    ):
        result.add_argument("--" + name.replace("_", "-"), required=True)
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(prepare(parser().parse_args())))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": type(exc).__name__,
                    "detail": str(exc)[:2500],
                }
            )
        )
        raise SystemExit(1)
