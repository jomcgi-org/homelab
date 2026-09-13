"""Focused render tests for the cluster-serving edge tier."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

_CHART_DIR = Path(__file__).resolve().parent


def _render(*settings: str) -> list[dict]:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    argv = [
        helm_bin,
        "template",
        "edge-test",
        str(_CHART_DIR),
        "--namespace",
        "ember-test",
    ]
    for setting in settings:
        argv += ["--set", setting]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _component(doc: dict) -> str | None:
    return doc.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")


def _one(docs: list[dict], kind: str, component: str) -> dict:
    matches = [
        doc for doc in docs if doc.get("kind") == kind and _component(doc) == component
    ]
    assert len(matches) == 1, f"wanted one {kind}/{component}, got {len(matches)}"
    return matches[0]


def _control_deployment(docs: list[dict]) -> dict:
    return next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment"
        and any(
            item.get("name") == "control-plane"
            for item in doc["spec"]["template"]["spec"]["containers"]
        )
    )


def test_edge_replicas_share_second_snapshot_and_span_nodes() -> None:
    docs = _render()
    edge = _one(docs, "Deployment", "serving-edge")
    pod = edge["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert edge["spec"]["replicas"] == 2
    assert container["args"][-3:] == [
        "embervm-serving-edge",
        "--service-cluster",
        edge["metadata"]["name"],
    ]
    assert (
        pod["topologySpreadConstraints"][0]["topologyKey"] == "kubernetes.io/hostname"
    )
    assert pod["topologySpreadConstraints"][0]["maxSkew"] == 1

    config = _one(docs, "ConfigMap", "serving-edge")["data"]["envoy-bootstrap.yaml"]
    assert "route_config_name: embervm-serving" in config
    assert "cluster_name: xds_cluster" in config
    assert ".svc.cluster.local" not in config


def test_static_gateway_route_targets_edge_while_edge_targets_node_service() -> None:
    docs = _render(
        "servingEnvoy.routes[0].enabled=true",
        "servingEnvoy.routes[0].name=ping",
        "servingEnvoy.routes[0].tier=private",
        "servingEnvoy.routes[0].gateway.name=private",
        "servingEnvoy.routes[0].gateway.namespace=envoy-gateway",
        "servingEnvoy.routes[0].hostname=example.test",
        "servingEnvoy.routes[0].pathPrefix=/ping",
        "servingEnvoy.routes[0].rewriteHost=ping.embervm.internal",
    )
    edge_service = _one(docs, "Service", "serving-edge")
    node_service = _one(docs, "Service", "serving-envoy")
    route = next(doc for doc in docs if doc.get("kind") == "HTTPRoute")
    backend = route["spec"]["rules"][0]["backendRefs"][0]

    assert backend == {"name": edge_service["metadata"]["name"], "port": 10000}
    assert (
        edge_service["spec"]["selector"]["app.kubernetes.io/component"]
        == "serving-edge"
    )
    assert (
        node_service["spec"]["selector"]["app.kubernetes.io/component"]
        == "serving-envoy"
    )

    control = _control_deployment(docs)
    control_container = next(
        item
        for item in control["spec"]["template"]["spec"]["containers"]
        if item["name"] == "control-plane"
    )
    env = {
        item["name"]: item["value"]
        for item in control_container["env"]
        if "value" in item
    }
    assert env["EMBERVM_SERVING_EDGE_NODE_ID"] == "embervm-serving-edge"
    assert env["EMBERVM_SERVING_EDGE_UPSTREAM_HOST"] == (
        f"{node_service['metadata']['name']}.ember-test.svc"
    )
    assert env["EMBERVM_SERVING_EDGE_UPSTREAM_PORT"] == "10000"

    # EndpointSlices remain owned by the Kubernetes Service controller. This
    # chart adds only static GitOps resources and no per-workload runtime object.
    assert all(doc.get("kind") != "EndpointSlice" for doc in docs)


def test_disabling_edge_removes_edge_resources_and_publisher_config() -> None:
    docs = _render("servingEnvoy.edge.enabled=false")
    assert not [doc for doc in docs if _component(doc) == "serving-edge"]

    control = _control_deployment(docs)
    container = next(
        item
        for item in control["spec"]["template"]["spec"]["containers"]
        if item["name"] == "control-plane"
    )
    env_names = {item["name"] for item in container["env"]}
    assert (
        not {
            "EMBERVM_SERVING_EDGE_NODE_ID",
            "EMBERVM_SERVING_EDGE_UPSTREAM_HOST",
            "EMBERVM_SERVING_EDGE_UPSTREAM_PORT",
        }
        & env_names
    )
