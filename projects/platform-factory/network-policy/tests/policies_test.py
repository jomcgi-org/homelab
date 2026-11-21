# invariant: PLAT-14
"""API server egress in the factory platform manifests and Helm releases."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml


PROJECTS = (
    Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "projects"
    if "TEST_SRCDIR" in os.environ
    else Path(__file__).resolve().parents[3]
)
PLATFORM = PROJECTS / "platform-factory"


def assert_api_ports(manifests):
    matches = []
    for doc in yaml.safe_load_all(manifests):
        if not doc or doc.get("kind") not in {
            "CiliumNetworkPolicy",
            "CiliumClusterwideNetworkPolicy",
        }:
            continue
        for spec in [doc.get("spec", {}), *doc.get("specs", [])]:
            for rule in spec.get("egress", []):
                if "kube-apiserver" in rule.get("toEntities", []):
                    name = doc["metadata"]["name"]
                    assert rule.get("toPorts") == [
                        {"ports": [{"port": "6443", "protocol": "TCP"}]}
                    ], name
                    matches.append(name)
    return matches


def test_platform_manifest_api_ports():
    matches = []
    for path in PLATFORM.glob("*/resources/*.yaml"):
        manifests = path.read_text()
        if "kube-apiserver" in manifests:
            matches.extend(assert_api_ports(manifests))
    assert matches


@pytest.mark.parametrize("release", ["edge", "external-secrets", "factory"])
def test_rendered_api_ports(release):
    chart = PLATFORM / release / "chart"
    args = []
    namespace = release
    if release == "factory":
        chart = PROJECTS / "factory/chart"
        args = [
            "-f",
            str(chart / "values-homelab.yaml"),
            "--set",
            "runbookRevert.enabled=true",
        ]
        namespace = "kagent"
    rendered = subprocess.check_output(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            release,
            str(chart),
            "-n",
            namespace,
            *args,
        ],
        text=True,
    )
    assert assert_api_ports(rendered)
