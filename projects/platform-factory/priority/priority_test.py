# invariant: PLAT-14
import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parent
CLASS = "factory-platform"


def test_platform_priority_class():
    resource = yaml.safe_load((ROOT / "priority-class.yaml").read_text())
    assert resource["kind"] == "PriorityClass"
    assert resource["metadata"]["name"] == CLASS
    assert resource["value"] == 100000
    assert resource["globalDefault"] is False
    assert resource["preemptionPolicy"] == "PreemptLowerPriority"


@pytest.mark.parametrize(
    "source,release,kind,name,path",
    [
        ("factory-db/resources/cluster.yaml", "", "Cluster", "factory-db", ""),
        ("kagent-postgres/resources/cluster.yaml", "", "Cluster", "kagent-pg", ""),
        ("garage/resources/garage.yaml", "", "StatefulSet", "garage", "template.spec"),
        ("../platform/authentik", "authentik", "Cluster", "authentik-pg", ""),
        (
            "../platform/onepassword",
            "connect",
            "Deployment",
            "onepassword-connect-operator",
            "template.spec",
        ),
        ("edge/chart", "edge", "Deployment", "envoy-gateway", "template.spec"),
        (
            "edge/chart",
            "edge",
            "EnvoyProxy",
            "edge",
            "provider.kubernetes.envoyDeployment.pod",
        ),
    ],
)
def test_platform_workload_priority(source, release, kind, name, path):
    source = ROOT.parent / source
    if release:
        command = [os.environ.get("HELM_BIN", "helm"), "template", release, str(source)]
        if release != "edge":
            command.extend(["-f", str(source / "values-factory.yaml")])
        manifests = subprocess.check_output(command, text=True)
    else:
        manifests = source.read_text()
    resources = {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(manifests)
        if doc
    }
    spec = resources[kind, name]["spec"]
    for field in filter(None, path.split(".")):
        spec = spec[field]
    assert spec["priorityClassName"] == CLASS
