"""Render the private landing route, runtime and network boundary."""

import json
import os
from pathlib import Path
import subprocess

import yaml

DIGEST = "sha256:" + "0" * 64


def test_private_status_app():
    chart = Path(__file__).resolve().parent
    result = subprocess.run(
        [
            os.environ["HELM_BIN"],
            "template",
            "factory-landing",
            str(chart),
            "--namespace",
            "factory-landing",
            "--set",
            f"image.digest={DIGEST}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    docs = {doc["kind"]: doc for doc in rendered}
    route = docs["HTTPRoute"]["spec"]
    assert route["hostnames"] == ["factory.jomcgi.dev"]
    assert route["parentRefs"] == [
        {
            "group": "gateway.networking.k8s.io",
            "kind": "Gateway",
            "name": "edge",
            "namespace": "edge",
        }
    ]
    assert "filters" not in route["rules"][0]
    assert route["rules"][0]["backendRefs"][0]["name"] == "factory-landing"
    pull_secret = docs["OnePasswordItem"]
    assert pull_secret["apiVersion"] == "onepassword.com/v1"
    assert pull_secret["type"] == "kubernetes.io/dockerconfigjson"
    assert pull_secret["metadata"]["name"] == "ghcr-imagepull-secret"
    assert pull_secret["metadata"]["namespace"] == "factory-landing"
    assert (
        pull_secret["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-2"
    )
    assert (
        pull_secret["spec"]["itemPath"]
        == "vaults/k8s-homelab/items/ghcr-read-permissions"
    )
    pod = docs["Deployment"]["spec"]["template"]["spec"]
    assert pod["imagePullSecrets"] == [{"name": "ghcr-imagepull-secret"}]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    assert container["image"].endswith(f"@{DIGEST}")
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert "envFrom" not in container
    assert container["env"] == [
        {
            "name": "CONNECTOR_STATUS_URL",
            "value": "http://factory-connector-status.factory.svc:8082/status",
        }
    ]
    policy = docs["CiliumNetworkPolicy"]["spec"]
    ports = [{"ports": [{"port": "8082", "protocol": "TCP"}]}]
    # The library's catalogued `connector` peer selects on name only.
    egress = [
        {
            "toEndpoints": [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "factory",
                        "app.kubernetes.io/name": "factory-connector",
                    }
                }
            ],
            "toPorts": ports,
        }
    ]
    assert policy["egress"] == egress
    peer = policy["ingress"][0]["fromEndpoints"][0]["matchLabels"]
    assert peer["k8s:io.kubernetes.pod.namespace"] == "edge"
    assert peer["gateway.envoyproxy.io/owning-gateway-name"] == "edge"

    # HOME-69: the pre-library objects at e68a9f09b7da, with a fixed test digest. Only
    # annotations change; pin selectors, probes, resources, routes and policies
    # as well as the security assertions above. HOME-24 adds only the status
    # URL and the connector egress checked above.
    before = json.loads((chart / "testdata/before.json").read_text())
    before["Deployment"]["spec"]["template"]["spec"]["containers"][0]["env"] = (
        container["env"]
    )
    before["CiliumNetworkPolicy"]["spec"]["egress"] = egress

    def without_annotations(value):
        if isinstance(value, dict):
            return {
                key: without_annotations(item)
                for key, item in value.items()
                if key != "annotations"
            }
        if isinstance(value, list):
            return [without_annotations(item) for item in value]
        return value

    assert without_annotations(docs) == without_annotations(before)
