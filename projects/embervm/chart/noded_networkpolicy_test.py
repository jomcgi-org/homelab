"""The noded NetworkPolicy must derive every activator port from the values
the rest of the system validates against.

On 2026-08-22 the first enable of this policy omitted the stateful and
composite activator TCP ranges entirely: serving-envoy's wake-on-connect to
noded:5401 was denied and demo-postgres stayed unwakeable for 14 minutes
while everything else looked healthy. The ranges were re-added by hand, but
nothing failed CI while they were missing. This file is that missing
failure.

The structural fix is that the template renders those ranges from
.Values.servingEnvoy.statefulTcpPortRange and
.Values.servingEnvoy.compositeTcpPortRange, which are the SAME capacity
blocks the CRD watcher validates stateful and composite listenPorts against.
These tests pin the derivation: move the values, and the policy must follow;
disable the policy, and nothing activator-shaped may leak out.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_CHART_DIR = Path(__file__).resolve().parent

_GRPC_PORT = 9090
_HEALTH_PORT = 8080
_ACTIVATOR_PORT = 8081
_SERVING_PORT_BASE = 30000
_STATEFUL_DEFAULT = (5400, 5409)
_COMPOSITE_DEFAULT = (5410, 5419)


def _render(settings: list[str]) -> str:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    argv = [helm_bin, "template", "np", str(_CHART_DIR), "--namespace", "np"]
    for setting in settings:
        argv += ["--set", setting]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return result.stdout


def _noded_policies(rendered: str) -> list[str]:
    return [
        d
        for d in rendered.split("\n---\n")
        if "kind: CiliumNetworkPolicy" in d and "-noded" in d and "tokenbroker" not in d
    ]


def _policy_doc(rendered: str) -> str:
    policies = _noded_policies(rendered)
    assert len(policies) == 1, (
        f"expected exactly one noded CiliumNetworkPolicy, got {len(policies)}"
    )
    return policies[0]


@pytest.fixture(scope="module")
def policy() -> str:
    return _policy_doc(_render(["noded.networkPolicy.enabled=true"]))


def test_disabled_renders_no_noded_policy() -> None:
    # The tokenbroker CNP is independent and always renders; only the noded
    # one is gated by networkPolicy.enabled.
    assert _noded_policies(_render([])) == []


def test_grpc_health_and_activator_ports_present(policy: str) -> None:
    for port in (_GRPC_PORT, _HEALTH_PORT, _ACTIVATOR_PORT):
        assert f'port: "{port}"' in policy, f"port {port} missing from the noded policy"


def test_serving_dnat_range_tracks_serving_port_base(policy: str) -> None:
    assert f'port: "{_SERVING_PORT_BASE + 2}"' in policy
    assert f"endPort: {_SERVING_PORT_BASE + 254}" in policy


def test_stateful_and_composite_ranges_match_serving_envoy_values(policy: str) -> None:
    start, end = _STATEFUL_DEFAULT
    assert f'port: "{start}"' in policy and f"endPort: {end}" in policy, (
        "stateful activator range missing from the policy; this is exactly "
        "the 2026-08-22 cold-wake drop"
    )
    cstart, cend = _COMPOSITE_DEFAULT
    assert f'port: "{cstart}"' in policy and f"endPort: {cend}" in policy, (
        "composite activator range missing from the policy"
    )


def test_ranges_follow_moved_values() -> None:
    """The structural pin: the policy renders FROM servingEnvoy's range values,
    so moving them moves the opened ports instead of silently leaving the old
    ones open (or worse, opening none)."""
    doc = _policy_doc(
        _render(
            [
                "noded.networkPolicy.enabled=true",
                "servingEnvoy.statefulTcpPortRange.start=5450",
                "servingEnvoy.statefulTcpPortRange.end=5459",
                "servingEnvoy.compositeTcpPortRange.start=5460",
                "servingEnvoy.compositeTcpPortRange.end=5469",
            ]
        )
    )
    assert 'port: "5450"' in doc and "endPort: 5459" in doc
    assert 'port: "5460"' in doc and "endPort: 5469" in doc
    assert 'port: "5400"' not in doc and 'port: "5410"' not in doc, (
        "stale default ranges still present after the values moved"
    )
