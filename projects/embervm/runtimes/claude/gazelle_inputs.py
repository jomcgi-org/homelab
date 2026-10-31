"""Fingerprint the source inputs of the image's repository Gazelle binary."""

import ast
import hashlib
import json
from pathlib import Path
import re
import sys


BINARY_MODULES = {
    "gazelle",
    "rules_go",
    "rules_python_gazelle_plugin",
    "bazel_skylib_gazelle_plugin",
    "bazel_skylib",
    "rules_python",
}
EXTENSION_GO_MODULES = {"sigs.k8s.io/yaml", "go.yaml.in/yaml/v2", "go.yaml.in/yaml/v3"}


def module_inputs(data):
    """Fingerprint generator dependencies, not unrelated images or charts.

    MODULE.bazel's declarations are Python-compatible syntax. Parse without
    evaluating it; retain relevant bazel_dep declarations and their overrides.
    """
    selected = []
    for node in ast.parse(data).body:
        call = node.value if isinstance(node, ast.Expr) else None
        if not isinstance(call, ast.Call):
            continue
        values = {
            arg.arg: arg.value.value
            for arg in call.keywords
            if isinstance(arg.value, ast.Constant)
        }
        name = values.get("name") or values.get("module_name")
        if name in BINARY_MODULES or values.get("path") in EXTENSION_GO_MODULES:
            selected.append(ast.dump(call))
    if not selected:
        raise ValueError("missing Gazelle module dependencies")
    return "\n".join(sorted(selected)).encode()


def digest(path):
    path = Path(path)
    data = path.read_bytes()
    if str(path) == "BUILD":
        declaration = re.search(rb"(?m)^gazelle_binary\(\n.*?^\)\n", data, re.S)
        if declaration is None:
            raise ValueError("cannot identify the repository Gazelle declaration")
        data = declaration[0]
    elif str(path) == "MODULE.bazel":
        data = module_inputs(data)
    elif str(path) in {"go.mod", "go.sum"}:
        # The repository extension imports sigs.k8s.io/yaml. Application Go
        # requirements are read from the outgoing tree by Gazelle itself.
        data = b"\n".join(
            line.strip()
            for line in data.splitlines()
            if line.split() and line.split()[0].decode() in EXTENSION_GO_MODULES
        )
        if not data:
            raise ValueError("missing Gazelle extension YAML dependency")
    return hashlib.sha256(data).hexdigest()


def verify(manifest):
    expected = json.loads(Path(manifest).read_text())
    changed = [
        p for p, sha in expected.items() if not Path(p).is_file() or digest(p) != sha
    ]
    if changed:
        raise RuntimeError(
            "packaged Gazelle inputs differ: "
            + ", ".join(changed)
            + "; rebuild the guest from this revision before validating"
        )


if __name__ == "__main__":
    if sys.argv[1] == "write":
        Path(sys.argv[2]).write_text(
            json.dumps({str(Path(p)): digest(p) for p in sys.argv[3:]}, sort_keys=True)
        )
    elif sys.argv[1] == "verify":
        verify(sys.argv[2])
    else:
        raise ValueError("expected write or verify")
