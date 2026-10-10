"""Render-test the control-plane feature-gate env wiring (issue #5230).

EMBERVM_PLACEMENT_RETRY and EMBERVM_ASYNC_LIFECYCLE_WRITES are read by the
control plane but were rendered by no template, so their docstrings'
"flips via a values-only deploy" claim described unreachable configuration.
This pins both env vars to their chart keys: rendered "false" by default
(the code's OFF default) and "true" when the key is set.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_ENV = re.compile(r'^\s*- name: (\S+)\n\s+value: "?([^"\n]+)"?', re.MULTILINE)
_GATES = (
    "EMBERVM_PLACEMENT_RETRY",
    "EMBERVM_ASYNC_LIFECYCLE_WRITES",
    "EMBERVM_DRAIN_INSTANCE_SCOPED_SESSIONS",
    "EMBERVM_CROSS_NODE_REJOIN",
    "EMBERVM_RESTART_FLUSH_INFLIGHT_INVOKES",
)


def _chart_dir() -> Path:
    chart = Path(__file__).resolve().parent
    if (chart / "Chart.yaml").exists():
        return chart
    raise RuntimeError("Could not find chart Chart.yaml")


def _render_env(sets: list[str]) -> dict[str, str]:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    result = subprocess.run(
        [
            helm_bin,
            "template",
            "gate-env-test",
            str(_chart_dir()),
            *[arg for pair in sets for arg in ("--set", pair)],
            "--show-only",
            "templates/deployment.yaml",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")

    return dict(_ENV.findall(result.stdout))


def _render_gate_values(sets: list[str]) -> dict[str, str]:
    env = _render_env(sets)
    missing = [name for name in _GATES if name not in env]
    assert not missing, f"gates not always rendered: {missing}"
    return {name: env[name] for name in _GATES}


def test_gates_default_off() -> None:
    assert _render_gate_values([]) == {
        "EMBERVM_PLACEMENT_RETRY": "false",
        "EMBERVM_ASYNC_LIFECYCLE_WRITES": "false",
        "EMBERVM_DRAIN_INSTANCE_SCOPED_SESSIONS": "false",
        "EMBERVM_CROSS_NODE_REJOIN": "false",
        "EMBERVM_RESTART_FLUSH_INFLIGHT_INVOKES": "false",
    }


def test_workspace_archive_ack_gate_is_staged_default_off() -> None:
    env = _render_env(["bricks.enabled=true"])
    assert "EMBERVM_BRICK_ARCHIVE_ACK_GATE" not in env
    assert "EMBERVM_BRICK_ARCHIVE_ACK_TIMEOUT_MS" not in env


def test_workspace_archive_ack_gate_uses_default_timeout_when_enabled() -> None:
    env = _render_env(
        ["bricks.enabled=true", "bricks.autoscale.archiveAckGate.enabled=true"]
    )
    assert env["EMBERVM_BRICK_ARCHIVE_ACK_GATE"] == "true"
    assert env["EMBERVM_BRICK_ARCHIVE_ACK_TIMEOUT_MS"] == "180000"


def test_workspace_archive_ack_gate_renders_enabled_and_custom_timeout() -> None:
    env = _render_env(
        [
            "bricks.enabled=true",
            "bricks.autoscale.archiveAckGate.enabled=true",
            "bricks.autoscale.archiveAckGate.timeoutMs=240000",
        ]
    )
    assert env["EMBERVM_BRICK_ARCHIVE_ACK_GATE"] == "true"
    assert env["EMBERVM_BRICK_ARCHIVE_ACK_TIMEOUT_MS"] == "240000"


def test_workspace_archive_ack_gate_is_inert_without_bricks() -> None:
    env = _render_env(["bricks.enabled=false"])
    assert "EMBERVM_BRICK_ARCHIVE_ACK_GATE" not in env
    assert "EMBERVM_BRICK_ARCHIVE_ACK_TIMEOUT_MS" not in env


@pytest.mark.parametrize(
    ("key", "env_name"),
    [
        ("dispatcher.placementRetry", "EMBERVM_PLACEMENT_RETRY"),
        ("asyncLifecycleWrites", "EMBERVM_ASYNC_LIFECYCLE_WRITES"),
        ("drainInstanceScopedSessions", "EMBERVM_DRAIN_INSTANCE_SCOPED_SESSIONS"),
        ("crossNodeRejoin", "EMBERVM_CROSS_NODE_REJOIN"),
        ("restartFlushInflightInvokes", "EMBERVM_RESTART_FLUSH_INFLIGHT_INVOKES"),
    ],
)
def test_gate_flips_via_its_chart_key(key: str, env_name: str) -> None:
    rendered = _render_gate_values([f"{key}=true"])
    assert rendered[env_name] == "true"
    for other in (name for name in _GATES if name != env_name):
        assert rendered[other] == "false", f"{key} must not move {other}"
