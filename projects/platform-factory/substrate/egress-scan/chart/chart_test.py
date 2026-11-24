"""Render-test the egress-scan chart.

The scanner takes no container args until the detector values arrive. The
Semgrep tier left the chart on 2026-11-24 (egress-scan/ARCHITECTURE.md
"Decided" 9), so its old values change nothing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

_CHART = Path(__file__).resolve().parent
_DIGEST = "sha256:" + "0" * 64


def _render(*sets: str) -> subprocess.CompletedProcess:
    args = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "egress-scan",
        str(_CHART),
        "--set",
        f"image.digest={_DIGEST}",
    ]
    for item in sets:
        args += ["--set", item]
    return subprocess.run(args, capture_output=True, text=True)


def _scanner(*sets: str) -> dict:
    result = _render(*sets)
    assert result.returncode == 0, result.stderr
    (deployment,) = [d for d in yaml.safe_load_all(result.stdout) if d]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    return container


def test_default_render_has_no_args_and_no_tier() -> None:
    result = _render()
    assert result.returncode == 0, result.stderr
    assert "semgrep" not in result.stdout
    assert "args" not in _scanner()


def test_the_removed_tier_values_change_nothing() -> None:
    baseline = _render().stdout
    old = _render(
        "semgrep.enabled=true",
        "semgrep.endpoint=http://engine.ate-system.svc:8080/",
    ).stdout
    assert old == baseline
