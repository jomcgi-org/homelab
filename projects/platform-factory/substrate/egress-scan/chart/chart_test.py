"""Render-test the second scan tier switch of the egress-scan chart.

With the tier off (the default) the Deployment has no container args. With it
on, the scanner receives the tier flags. A bad value fails the render.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

_CHART = Path(__file__).resolve().parent
_DIGEST = "sha256:" + "0" * 64
_ENDPOINT = "http://engine.ate-system.svc:8080/"


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


def test_tier_is_off_by_default() -> None:
    values = yaml.safe_load((_CHART / "values.yaml").read_text())["semgrep"]
    assert values["enabled"] is False
    assert values["failureMode"] == "deny"
    assert values["deadline"] == "300ms"
    assert values["maxBodyBytes"] == 524288


def test_default_render_has_no_tier_flags() -> None:
    result = _render()
    assert result.returncode == 0, result.stderr
    assert "semgrep" not in result.stdout
    assert "args" not in _scanner()


def test_endpoint_and_tuning_are_ignored_while_the_tier_is_off() -> None:
    baseline = _render().stdout
    tuned = _render(
        f"semgrep.endpoint={_ENDPOINT}",
        "semgrep.deadline=75ms",
        "semgrep.concurrency=2",
    ).stdout
    assert tuned == baseline


def test_enabled_tier_passes_every_flag() -> None:
    args = _scanner("semgrep.enabled=true", f"semgrep.endpoint={_ENDPOINT}")["args"]
    assert args == [
        "--semgrep-enabled",
        f"--semgrep-endpoint={_ENDPOINT}",
        "--semgrep-deadline=300ms",
        "--semgrep-failure-mode=deny",
        "--semgrep-max-body=524288",
        "--semgrep-concurrency=8",
        "--semgrep-max-response=1048576",
        "--semgrep-breaker-failures=5",
        "--semgrep-breaker-cooldown=5s",
    ]


def test_enabled_tier_follows_the_values() -> None:
    args = _scanner(
        "semgrep.enabled=true",
        f"semgrep.endpoint={_ENDPOINT}",
        "semgrep.deadline=75ms",
        "semgrep.failureMode=allow",
        "semgrep.breaker.failures=0",
    )["args"]
    assert "--semgrep-deadline=75ms" in args
    assert "--semgrep-failure-mode=allow" in args
    assert "--semgrep-breaker-failures=0" in args


def test_enabled_tier_needs_an_endpoint() -> None:
    result = _render("semgrep.enabled=true")
    assert result.returncode != 0
    assert "semgrep.endpoint is required" in result.stderr


def test_failure_mode_must_be_deny_or_allow() -> None:
    result = _render(
        "semgrep.enabled=true",
        f"semgrep.endpoint={_ENDPOINT}",
        "semgrep.failureMode=ignore",
    )
    assert result.returncode != 0
    assert "failureMode must be deny or allow" in result.stderr
