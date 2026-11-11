"""Rendered contract for the CPU embeddings chart after NInfer retirement."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml


CHART = Path(__file__).resolve().parent


def render(gke, release="inference", overrides=()):
    command = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        release,
        str(CHART),
        "--namespace",
        "inference",
    ]
    if gke:
        command += ["-f", str(CHART / "values-gke.yaml")]
    for override in overrides:
        command += ["--set", override]
    output = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    return [doc for doc in yaml.safe_load_all(output) if doc]


@pytest.mark.parametrize("gke", [False, True])
@pytest.mark.parametrize(
    "release,overrides,name,label",
    [
        ("inference", (), "inference", "inference"),
        ("example", ("fullnameOverride=",), "example-inference", "inference"),
        (
            "example",
            ("fullnameOverride=", "nameOverride=embed"),
            "example-embed",
            "embed",
        ),
        ("example", ("fullnameOverride=custom",), "custom", "inference"),
    ],
)
def test_embeddings_only(gke, release, overrides, name, label):
    docs = render(gke, release, overrides)
    # An exact resource set catches generative deployments/services, NodePorts,
    # HTTPRoutes and ingress policies even if their names change.
    assert {(doc["kind"], doc["metadata"]["name"]) for doc in docs} == {
        ("Deployment", name + "-embeddings"),
        ("Service", name + "-embeddings"),
        ("OnePasswordItem", "ghcr-imagepull-secret"),
    }
    assert len(docs) == 3
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    service = next(doc for doc in docs if doc["kind"] == "Service")
    secret = next(doc for doc in docs if doc["kind"] == "OnePasswordItem")
    selector = {
        "app.kubernetes.io/name": label,
        "app.kubernetes.io/instance": release,
        "app.kubernetes.io/component": "embeddings",
    }
    assert service["spec"] == {
        "type": "ClusterIP",
        "ports": [
            {"port": 8080, "targetPort": "http", "protocol": "TCP", "name": "http"}
        ],
        "selector": selector,
    }
    assert secret["spec"] == {
        "itemPath": "vaults/k8s-homelab/items/ghcr-read-permissions"
    }
    assert secret["type"] == "kubernetes.io/dockerconfigjson"
    spec = deployment["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"] == {"type": "Recreate"}
    assert spec["selector"]["matchLabels"] == selector
    assert spec["template"]["metadata"]["labels"] == selector
    pod = spec["template"]["spec"]
    assert "runtimeClassName" not in pod
    assert pod["imagePullSecrets"] == [{"name": "ghcr-imagepull-secret"}]
    assert pod["securityContext"] == {
        "runAsUser": 0,
        "runAsGroup": 0,
        "fsGroup": 0,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert pod["nodeSelector"] == (
        {"embervm.io/serving": "true"} if gke else {"kubernetes.io/hostname": "node-4"}
    )
    assert pod.get("tolerations", []) == (
        [
            {
                "key": "embervm.jomcgi.dev/node",
                "operator": "Equal",
                "value": "true",
                "effect": "NoSchedule",
            }
        ]
        if gke
        else []
    )
    assert pod["volumes"] == [
        {
            "name": "model",
            "image": {
                "reference": "ghcr.io/jomcgi/models/voyageai/voyage-4-nano:jsonmartin-gguf-q8-0",
                "pullPolicy": "IfNotPresent",
            },
        },
        {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"}},
    ]
    assert len(pod["containers"]) == 1
    container = pod["containers"][0]
    assert container["name"] == "llama-server"
    assert container["image"] == (
        "ghcr.io/ggml-org/llama.cpp:server-cuda-b8643@sha256:"
        "ee54620034076d53fee9cd6acf8483902becd3d66acf77acc6ecfe5eefddaecb"
    )
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": False,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["ports"] == [
        {"name": "http", "containerPort": 8080, "protocol": "TCP"}
    ]
    assert container["resources"] == {
        "requests": {"cpu": 1 if gke else 2, "memory": "2Gi" if gke else "12Gi"},
        "limits": {"memory": "12Gi"},
    }
    assert container["volumeMounts"] == [
        {"name": "model", "mountPath": "/model-image", "readOnly": True},
        {"name": "dshm", "mountPath": "/dev/shm"},
    ]
    assert container["startupProbe"] == {
        "httpGet": {"path": "/health", "port": "http"},
        "initialDelaySeconds": 10,
        "periodSeconds": 5,
        "failureThreshold": 60,
    }
    for probe in ("livenessProbe", "readinessProbe"):
        assert container[probe] == {
            "httpGet": {"path": "/health", "port": "http"},
            "periodSeconds": 10,
            "failureThreshold": 3,
        }
    assert container["command"] == ["/bin/sh", "-c"]
    (script,) = container["args"]
    assert "find /model-image -maxdepth 2 -name '*.gguf' ! -name 'mmproj*'" in script
    assert 'if [ -z "$MODEL" ]; then' in script
    assert "exit 1" in script
    assert shlex.split(
        script.split("exec /app/llama-server", 1)[1].replace("\\\n", " ")
    ) == [
        "--model",
        "$MODEL",
        "--n-gpu-layers",
        "0",
        "--ctx-size",
        "8192",
        "--flash-attn",
        "off",
        "--cache-type-k",
        "f16",
        "--cache-type-v",
        "f16",
        "--threads",
        "4",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
        "--embedding",
        "--alias",
        "voyage-4-nano",
        "--metrics",
        "--ubatch-size",
        "8192",
        "--batch-size",
        "8192",
    ]


@pytest.mark.parametrize("gke", [False, True])
def test_embeddings_disabled(gke):
    docs = render(gke, overrides=("embeddings.enabled=false",))
    assert [doc["kind"] for doc in docs] == ["OnePasswordItem"]
    assert (
        render(
            gke, overrides=("embeddings.enabled=false", "imagePullSecret.enabled=false")
        )
        == []
    )


@pytest.mark.parametrize("gke", [False, True])
def test_retired_options_cannot_restore_generative_resources(gke):
    assert render(
        gke,
        overrides=(
            "ninfer.enabled=true",
            "benchMode.enabled=true",
            "cfIngress.enabled=true",
        ),
    ) == render(gke)
