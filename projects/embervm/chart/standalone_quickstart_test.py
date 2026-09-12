import os
import subprocess
from pathlib import Path

import yaml


CHART_DIR = Path(__file__).parent
VALUES = CHART_DIR / "standalone-values.yaml"


def _render() -> str:
    helm = os.environ.get("HELM_BIN", "helm")
    return subprocess.run(
        [
            helm,
            "template",
            "embervm",
            str(CHART_DIR),
            "--namespace",
            "embervm",
            "--values",
            str(VALUES),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _objects(rendered: str) -> list[dict]:
    return [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]


def _kind(objects: list[dict], kind: str) -> list[dict]:
    return [item for item in objects if item.get("kind") == kind]


def test_standalone_profile_renders_only_the_minimum_execution_lane():
    rendered = _render()
    objects = _objects(rendered)

    assert [item["metadata"]["name"] for item in _kind(objects, "Deployment")] == [
        "embervm-embervm"
    ]
    assert [item["metadata"]["name"] for item in _kind(objects, "DaemonSet")] == [
        "embervm-embervm-noded"
    ]
    assert [item["metadata"]["name"] for item in _kind(objects, "Service")] == [
        "embervm-embervm-noded",
        "embervm-embervm",
    ]
    assert [item["metadata"]["name"] for item in _kind(objects, "Workload")] == [
        "sandbox-python"
    ]
    assert not _kind(objects, "OnePasswordItem")
    assert not _kind(objects, "HTTPRoute")

    noded = _kind(objects, "DaemonSet")[0]
    pod = noded["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"embervm.jomcgi.dev/node": "true"}
    assert pod["priorityClassName"] == "embervm-quickstart"
    assert [item["name"] for item in pod["initContainers"]] == [
        "build-sandbox-python-rootfs"
    ]
    assert [item["name"] for item in pod["containers"]] == ["noded"]

    control = _kind(objects, "Deployment")[0]
    containers = control["spec"]["template"]["spec"]["containers"]
    assert [item["name"] for item in containers] == ["control-plane"]

    pvc = _kind(objects, "PersistentVolumeClaim")
    assert len(pvc) == 1
    assert pvc[0]["spec"]["storageClassName"] == "local-path"

    assert "fc-agentd" not in rendered
    object_data = yaml.safe_dump_all(objects).lower()
    for homelab_dependency in (
        "onepassword.com",
        "seaweedfs",
        "cloudflare",
        "longhorn",
        "homelab-disposable",
    ):
        assert homelab_dependency not in object_data


def test_standalone_profile_keeps_management_auth_and_local_only_store_explicit():
    rendered = _render()
    objects = _objects(rendered)
    control = _kind(objects, "Deployment")[0]
    control_env = {
        item["name"]: item.get("value")
        for item in control["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert control_env["EMBERVM_ALLOWED_SERVICE_ACCOUNTS"] == (
        "system:serviceaccount:embervm:quickstart-client"
    )

    noded = _kind(objects, "DaemonSet")[0]
    noded_env = {
        item["name"]: item.get("value")
        for item in noded["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert noded_env["EMBERVM_NODED_STORE_ENDPOINT"] == ""
    assert noded_env["EMBERVM_NODED_STORE_BUCKET"] == "embervm-quickstart"
