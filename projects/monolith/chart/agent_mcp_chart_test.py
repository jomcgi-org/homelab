"""Rendered-chart guards for the Ember agent MCP sidecar."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def _render() -> list[dict]:
    chart_dir = Path(__file__).resolve().parent
    deploy_values = os.environ.get(
        "DEPLOY_VALUES", str(chart_dir.parent / "deploy" / "values.yaml")
    )
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "kg",
            str(chart_dir),
            "-f",
            deploy_values,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _app_deployment(documents: list[dict]) -> dict:
    deployments = [
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and any(
            container.get("name") == "agent-mcp"
            for container in document["spec"]["template"]["spec"]["containers"]
        )
    ]
    assert len(deployments) == 1
    return deployments[0]


def test_agent_mcp_container_has_isolated_port_and_liveness_only():
    deployment = _app_deployment(_render())
    container = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "agent-mcp"
    )

    assert container["ports"] == [
        {"name": "agent-mcp", "containerPort": 8092, "protocol": "TCP"}
    ]
    assert container["livenessProbe"] == {
        "httpGet": {"path": "/healthz", "port": "agent-mcp"},
        "initialDelaySeconds": 5,
        "periodSeconds": 20,
        "timeoutSeconds": 5,
        "failureThreshold": 6,
    }
    assert "readinessProbe" not in container
    assert {item["name"] for item in container["env"]} == {
        "DATABASE_URL",
        "EMBEDDING_URL",
        "KNOWLEDGE_DEFAULT_REPO_SCOPE",
        "AUTH_AUTHENTIK_JWKS_URL",
        "AUTH_AUTHENTIK_ISSUER",
        "AUTH_AUTHENTIK_AUDIENCE",
        "AUTH_AUTHENTIK_AGENT_JWKS_URL",
        "AUTH_AUTHENTIK_AGENT_ISSUER",
        "AUTH_AUTHENTIK_AGENT_AUDIENCE",
        "AUTH_JWKS_CACHE_TTL_S",
        # Object storage for raw bodies; see the dedicated test below for why
        # their absence would be a silent loss rather than a visible failure.
        "SEAWEEDFS_S3_ENDPOINT",
        "AWS_REGION",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
    }


def test_auth_env_vars_present_on_both_containers():
    deployment = _app_deployment(_render())
    containers = {
        container["name"]: container
        for container in deployment["spec"]["template"]["spec"]["containers"]
    }

    expected = {
        "AUTH_AUTHENTIK_JWKS_URL",
        "AUTH_AUTHENTIK_ISSUER",
        "AUTH_AUTHENTIK_AUDIENCE",
        "AUTH_AUTHENTIK_AGENT_JWKS_URL",
        "AUTH_AUTHENTIK_AGENT_ISSUER",
        "AUTH_AUTHENTIK_AGENT_AUDIENCE",
    }
    for name in ("backend", "agent-mcp"):
        env_names = {item["name"] for item in containers[name]["env"]}
        assert expected <= env_names


def test_service_exposes_agent_mcp_port():
    services = [document for document in _render() if document.get("kind") == "Service"]
    service = next(
        document
        for document in services
        if document.get("metadata", {}).get("name") == "kg"
    )

    assert {
        "name": "agent-mcp",
        "port": 8092,
        "targetPort": "agent-mcp",
        "protocol": "TCP",
    } in service["spec"]["ports"]


def test_agent_mcp_container_can_upload_raw_bodies():
    """report_knowledge writes the raw body to object storage and extraction
    reads it back. raw_store.upload_raw silently no-ops when the endpoint env
    is absent, so a container missing these would accept every report, persist
    a Postgres row with no body, and lose it. Assert the wiring, not the code.
    """

    deployment = _app_deployment(_render())
    container = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "agent-mcp"
    )
    names = {env["name"] for env in container["env"]}
    assert {
        "SEAWEEDFS_S3_ENDPOINT",
        "AWS_REGION",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
    } <= names
