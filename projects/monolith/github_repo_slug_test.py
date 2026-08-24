"""Guard against reintroducing the repository's stale GitHub slug."""

from __future__ import annotations

import ast
from pathlib import Path

_MONOLITH_ROOT = Path(__file__).resolve().parent
_STALE_REPO = "jomcgi/homelab"


# A bazel test only sees its own runfiles, so this walk covers exactly what the
# target's `data` glob puts there. An empty collection would make the assertion
# below vacuously true and the guard would protect nothing, which is the failure
# mode this floor exists to catch. Well under the ~380 non-test sources present
# when this landed, so ordinary churn does not trip it.
_MIN_SCANNED_FILES = 200


def test_non_test_python_files_do_not_use_stale_github_repo_constant():
    violations: list[str] = []
    scanned = 0

    for py_file in sorted(_MONOLITH_ROOT.rglob("*.py")):
        if py_file.name.endswith("_test.py") or py_file.name.startswith("test_"):
            continue

        scanned += 1
        rel = py_file.relative_to(_MONOLITH_ROOT)
        tree = ast.parse(py_file.read_text(), filename=str(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == _STALE_REPO:
                violations.append(f"  {rel}:{node.lineno}")

    assert scanned >= _MIN_SCANNED_FILES, (
        f"only {scanned} source file(s) reached this test, expected at least "
        f"{_MIN_SCANNED_FILES}. The runfiles tree is incomplete, so this guard "
        "is not actually scanning anything. Check the `data` glob on the "
        "github_repo_slug_test target in projects/monolith/BUILD."
    )
    assert not violations, (
        "Stale GitHub repository constants found:\n"
        + "\n".join(violations)
        + "\nUse core.github.GITHUB_REPO instead."
    )
