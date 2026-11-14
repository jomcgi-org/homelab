"""Render the private landing route, runtime and network boundary."""

import os
from pathlib import Path
import subprocess

import yaml


def test_private_static_app():
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
            "image.digest=sha256:test",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    docs = {doc["kind"]: doc for doc in yaml.safe_load_all(result.stdout) if doc}
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
    pod = docs["Deployment"]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    assert container["image"].endswith("@sha256:test")
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert "envFrom" not in container
    policy = docs["CiliumNetworkPolicy"]["spec"]
    assert "egress" not in policy
    peer = policy["ingress"][0]["fromEndpoints"][0]["matchLabels"]
    assert peer["k8s:io.kubernetes.pod.namespace"] == "edge"
    assert peer["gateway.envoyproxy.io/owning-gateway-name"] == "edge"
