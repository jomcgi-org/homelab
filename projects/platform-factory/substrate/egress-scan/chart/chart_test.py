"""Render-test the egress-scan chart.

The scanner gets one detector, chosen by name, and its mode, failure mode and
limits. The Semgrep tier left the chart on 2026-11-24 (egress-scan/ARCHITECTURE.md
"Decided" 9 and 10), so its old values change nothing.
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


def test_default_render_selects_gitleaks_with_the_documented_limits() -> None:
    result = _render()
    assert result.returncode == 0, result.stderr
    assert "semgrep" not in result.stdout
    assert _scanner()["args"] == [
        "--detector=gitleaks",
        "--detector-mode=enforce",
        "--detector-failure-mode=deny",
        "--detector-deadline=75ms",
        "--detector-max-input-bytes=524288",
        "--metrics-routes=hindsight-api.hindsight.svc.cluster.local:8888",
        "--metrics-max-label-values=32",
    ]


def test_the_detector_name_and_limits_follow_the_values() -> None:
    args = _scanner(
        "detector.name=betterleaks",
        "detector.mode=observe",
        "detector.failureMode=allow",
        "detector.deadline=60ms",
        "detector.maxInputBytes=65536",
    )["args"]
    assert args == [
        "--detector=betterleaks",
        "--detector-mode=observe",
        "--detector-failure-mode=allow",
        "--detector-deadline=60ms",
        "--detector-max-input-bytes=65536",
        "--metrics-routes=hindsight-api.hindsight.svc.cluster.local:8888",
        "--metrics-max-label-values=32",
    ]


def test_a_data_path_is_passed_only_when_set() -> None:
    assert not [a for a in _scanner()["args"] if "data-path" in a]
    args = _scanner("detector.dataPath=/data/index")["args"]
    assert "--detector-data-path=/data/index" in args


def test_a_bad_mode_or_failure_mode_fails_the_render() -> None:
    for sets, message in (
        (["detector.mode=audit"], "detector.mode must be enforce or observe"),
        (["detector.failureMode=ignore"], "detector.failureMode must be deny or allow"),
        (["detector.name="], "detector.name is required"),
    ):
        result = _render(*sets)
        assert result.returncode != 0
        assert message in result.stderr


def test_the_removed_tier_values_change_nothing() -> None:
    baseline = _render().stdout
    old = _render(
        "semgrep.enabled=true",
        "semgrep.endpoint=http://engine.ate-system.svc:8080/",
    ).stdout
    assert old == baseline


def test_the_route_allowlist_and_the_cap_follow_the_values() -> None:
    args = _scanner("metrics.routes={a.example.com:443,b.example.com}", "metrics.maxLabelValues=8")["args"]
    assert "--metrics-routes=a.example.com:443,b.example.com" in args
    assert "--metrics-max-label-values=8" in args
    # No allowlist: the scanner falls back to the cap on distinct values.
    args = _scanner("metrics.routes=null")["args"]
    assert not [a for a in args if a.startswith("--metrics-routes")]
    assert "--metrics-max-label-values=32" in args
