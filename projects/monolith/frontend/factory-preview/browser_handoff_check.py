"""Mandatory independent-review preflight, staged by the trusted guest shim.

The shim verifies this script and its import closure against server-pinned
hashes. Restored files are data only; no restored code, venv or startup hook is
loaded. The manifest must include before/after/results.json, provenance.json,
and inputs.json mapping fixture/checklist/dependency_lock/browser_binary to
inventory paths. This command proves integrity, never visual acceptance.
"""

import importlib.util
import json
from pathlib import Path
import sys


def main():
    staged = Path(__file__).resolve()
    sys.path.insert(0, str(staged.parent))
    import sheet_check

    contract_path = staged.parents[2] / "factory/orchestration/browser_evidence.py"
    spec = importlib.util.spec_from_file_location("browser_evidence", contract_path)
    contract = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = contract
    spec.loader.exec_module(contract)
    request = json.load(sys.stdin)
    raw = request["handoff"]
    handoff = contract.parse_handoff(
        json.dumps(raw).encode(),
        expected_provenance=contract.Provenance(**raw["provenance"]),
        expected_producer=contract.Producer(**raw["producer"]),
    )
    checkout = Path(request["checkout"])
    verified = contract.verify_inventory(str(checkout), handoff)
    root = checkout / handoff.evidence_root
    manifest = json.loads((root / handoff.manifest_path).read_text())
    entries = {entry["path"]: entry for entry in manifest["files"]}
    required = {
        "before/results.json",
        "after/results.json",
        "provenance.json",
        "inputs.json",
    }
    if not required.issubset(entries):
        raise ValueError("missing required capture inventory")
    provenance = json.loads((root / "provenance.json").read_text())
    for name, expected in [
        ("base_sha", handoff.provenance.base_sha),
        ("target_sha", handoff.provenance.target_sha),
        ("head_sha", handoff.provenance.head_sha),
        ("harness_sha", handoff.provenance.harness_sha),
    ]:
        if provenance.get(name) != expected:
            raise ValueError("stale comparison provenance: " + name)
    inputs = json.loads((root / "inputs.json").read_text())
    for kind in ("fixture", "checklist", "dependency_lock", "browser_binary"):
        name = inputs[kind]
        if name not in entries or entries[name]["sha256"] != getattr(
            handoff.provenance, kind + "_sha256"
        ):
            raise ValueError("missing or mismatched capture input: " + kind)
    for side, revision in [
        ("before", handoff.provenance.base_sha),
        ("after", handoff.provenance.head_sha),
    ]:
        result = json.loads((root / side / "results.json").read_text())
        if (
            result["revision"] != revision
            or result["build"]["harness_sha"] != handoff.provenance.harness_sha
            or result["chromium_version"] != handoff.provenance.browser_version
            or handoff.provenance.browser_name != "chromium"
        ):
            raise ValueError("stale source, harness or browser identity")
        for record in result["captures"]:
            for name in record["repeatability_images"]:
                if (
                    not isinstance(name, str)
                    or "/" in name
                    or "\\" in name
                    or not name.endswith(".png")
                    or f"{side}/{name}" not in entries
                ):
                    raise ValueError("capture references an unverified path")
        # Reject paths not in the verified inventory before the harness opens
        # them; all resulting screenshots, manifests and traces must be bound.
        for path in (root / side).rglob("*"):
            if path.is_symlink() or (
                path.is_file() and path.relative_to(root).as_posix() not in entries
            ):
                raise ValueError("unverified file in capture bundle")
        sheet_check.validate_bundle(root / side, result)
    print(
        json.dumps(
            {
                "status": "ok",
                "handoff_sha256": handoff.handoff_sha256,
                "manifest_sha256": verified.manifest_sha256,
                "inventory_sha256": verified.inventory_sha256,
                "base_sha": handoff.provenance.base_sha,
                "head_sha": handoff.provenance.head_sha,
                "harness_sha": handoff.provenance.harness_sha,
                "files": verified.files,
                "bytes": verified.bytes,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": type(exc).__name__,
                    "detail": str(exc)[:1000],
                }
            )
        )
        raise SystemExit(1)
