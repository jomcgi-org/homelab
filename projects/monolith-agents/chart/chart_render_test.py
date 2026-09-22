"""Render guards for the isolated monolith-agents chart."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


REQUIRED_ENV = {
    "DATABASE_URL",
    "EMBEDDING_URL",
    "KNOWLEDGE_DEFAULT_REPO_SCOPE",
    "AUTH_AUTHENTIK_JWKS_URL",
    "AUTH_AUTHENTIK_ISSUER",
    "AUTH_AUTHENTIK_AUDIENCE",
    "AUTH_JWKS_CACHE_TTL_S",
    "AUTH_AUTHENTIK_AGENT_JWKS_URL",
    "AUTH_AUTHENTIK_AGENT_ISSUER",
    "AUTH_AUTHENTIK_AGENT_AUDIENCE",
    "SEAWEEDFS_S3_ENDPOINT",
    "AWS_REGION",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID",
}


def _chart_dir() -> Path:
    chart = Path(__file__).resolve().parent
    if not (chart / "Chart.yaml").exists():
        raise RuntimeError("Could not find monolith-agents Chart.yaml")
    return chart


@pytest.fixture(scope="module")
def documents() -> list[dict]:
    helm = os.environ.get("HELM_BIN", "helm")
    chart = _chart_dir()
    deploy_values = Path(
        os.environ.get("DEPLOY_VALUES", chart.parent / "deploy/values.yaml")
    )
    result = subprocess.run(
        [
            helm,
            "template",
            "monolith-agents",
            str(chart),
            "--namespace",
            "monolith-agents",
            "--values",
            str(deploy_values),
            # Render ARMED. deploy/values.yaml keeps agents.enabled false until
            # the tier's database credential exists, but these tests are about
            # what the workload looks like once it runs, so they must not
            # silently pass by rendering no Deployment at all.
            "--set",
            "agents.enabled=true",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


@pytest.fixture(scope="module")
def hub_documents() -> list[dict]:
    """The manifest that actually ships to the GKE hub: disarmed, hub overrides."""
    helm = os.environ.get("HELM_BIN", "helm")
    chart = _chart_dir()
    deploy = chart.parent / "deploy"
    result = subprocess.run(
        [
            helm,
            "template",
            "monolith-agents",
            str(chart),
            "--namespace",
            "monolith-agents",
            "--values",
            str(os.environ.get("DEPLOY_VALUES", deploy / "values.yaml")),
            "--values",
            str(os.environ.get("DEPLOY_VALUES_GKE", deploy / "values-gke.yaml")),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _deployment(documents: list[dict]) -> dict:
    return next(doc for doc in documents if doc.get("kind") == "Deployment")


OBSERVATION_NAMESPACES = {
    "argocd",
    "authentik",
    "embervm",
    "inference",
    "kargo",
    "kargo-embervm",
    "kargo-monolith",
    "mcp",
    "monolith",
    "monolith-agents",
    "otel-collector",
}
GENERAL_RULES = [
    {
        "apiGroups": [""],
        "resources": ["pods", "services", "configmaps", "events"],
        "verbs": ["get", "list"],
    },
    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    {
        "apiGroups": ["apps"],
        "resources": [
            "deployments",
            "statefulsets",
            "daemonsets",
            "replicasets",
        ],
        "verbs": ["get", "list"],
    },
    {
        "apiGroups": ["metrics.k8s.io"],
        "resources": ["pods"],
        "verbs": ["get", "list"],
    },
]


def test_agents_tier_has_exact_restricted_read_rbac(documents: list[dict]) -> None:
    roles = [doc for doc in documents if doc.get("kind") == "Role"]
    rolebindings = [doc for doc in documents if doc.get("kind") == "RoleBinding"]
    clusterroles = [doc for doc in documents if doc.get("kind") == "ClusterRole"]
    clusterrolebindings = [
        doc for doc in documents if doc.get("kind") == "ClusterRoleBinding"
    ]

    general_roles = [
        role for role in roles if role["metadata"]["name"].endswith("-observer")
    ]
    assert {role["metadata"]["namespace"] for role in general_roles} == (
        OBSERVATION_NAMESPACES
    )
    assert all(role["rules"] == GENERAL_RULES for role in general_roles)

    argocd = next(
        role for role in roles if role["metadata"]["name"].endswith("-argocd-reader")
    )
    assert argocd["metadata"]["namespace"] == "argocd"
    assert argocd["rules"] == [
        {
            "apiGroups": ["argoproj.io"],
            "resources": ["applications"],
            "verbs": ["get", "list"],
        }
    ]

    kargo = [
        role for role in roles if role["metadata"]["name"].endswith("-kargo-reader")
    ]
    assert {role["metadata"]["namespace"] for role in kargo} == {
        "kargo-embervm",
        "kargo-monolith",
    }
    assert all(
        role["rules"]
        == [
            {
                "apiGroups": ["kargo.akuity.io"],
                "resources": ["freights"],
                "verbs": ["get", "list"],
            }
        ]
        for role in kargo
    )

    assert len(roles) == len(OBSERVATION_NAMESPACES) + 3
    assert len(rolebindings) == len(roles)
    assert len(clusterroles) == 1
    assert clusterroles[0]["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["nodes", "namespaces"],
            "verbs": ["get", "list"],
        },
        {
            "apiGroups": ["metrics.k8s.io"],
            "resources": ["nodes"],
            "verbs": ["get", "list"],
        },
    ]
    assert len(clusterrolebindings) == 1

    all_rules = [rule for role in [*roles, *clusterroles] for rule in role["rules"]]
    assert not any("*" in rule["apiGroups"] for rule in all_rules)
    assert not any("*" in rule["resources"] for rule in all_rules)
    assert not any(
        verb not in {"get", "list"} for rule in all_rules for verb in rule["verbs"]
    )
    assert not any(
        resource in {"secrets", "pods/exec", "pods/attach", "pods/portforward"}
        or resource.endswith("/proxy")
        for rule in all_rules
        for resource in rule["resources"]
    )

    bindings = [*rolebindings, *clusterrolebindings]
    assert all(
        binding["subjects"]
        == [
            {
                "kind": "ServiceAccount",
                "name": "monolith-agents",
                "namespace": "monolith-agents",
            }
        ]
        for binding in bindings
    )
    assert all(
        binding["roleRef"]
        == {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": binding["metadata"]["name"],
        }
        for binding in rolebindings
    )
    assert clusterrolebindings[0]["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": clusterroles[0]["metadata"]["name"],
    }
    assert clusterroles[0]["metadata"]["name"].startswith("monolith-agents-")


def test_agents_container_exposes_port_8092(documents: list[dict]) -> None:
    container = _deployment(documents)["spec"]["template"]["spec"]["containers"][0]
    assert {port["containerPort"] for port in container["ports"]} == {8092}


def test_healthz_probe_is_present(documents: list[dict]) -> None:
    container = _deployment(documents)["spec"]["template"]["spec"]["containers"][0]
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"


def test_all_required_environment_variables_are_present(documents: list[dict]) -> None:
    # Object-storage and agent-auth sets fail SILENTLY when missing, which is why they are asserted.
    container = _deployment(documents)["spec"]["template"]["spec"]["containers"][0]
    env_names = {entry["name"] for entry in container["env"]}
    assert REQUIRED_ENV <= env_names


def _secret_refs(container: dict) -> set[str]:
    return {
        entry["valueFrom"]["secretKeyRef"]["name"]
        for entry in container["env"]
        if "valueFrom" in entry and "secretKeyRef" in entry["valueFrom"]
    }


def test_every_secret_ref_is_synced_into_the_namespace(documents: list[dict]) -> None:
    # Secrets are namespaced. A secretKeyRef that no OnePasswordItem in this
    # chart syncs resolves to nothing at runtime: none of these refs are
    # optional, so the pod sits in CreateContainerConfigError.
    container = _deployment(documents)["spec"]["template"]["spec"]["containers"][0]
    synced = {
        doc["metadata"]["name"]
        for doc in documents
        if doc.get("kind") == "OnePasswordItem"
    }
    assert _secret_refs(container) <= synced, (
        f"unsynced secret refs: {_secret_refs(container) - synced}"
    )
    assert {"monolith-agents-db", "monolith-r2-s3"} <= synced


def test_hub_render_holds_its_invariants_armed_or_not(
    hub_documents: list[dict],
) -> None:
    # What ships to the GKE hub, at whatever agents.enabled the deploy values
    # carry. No CiliumNetworkPolicy (the hub has no CRD for it, and the first
    # sync would wedge), both OnePasswordItems so the Secrets exist before or
    # alongside the workload, and if a Deployment renders, every Secret it
    # reads is one of those.
    kinds = {doc["kind"] for doc in hub_documents}
    assert "CiliumNetworkPolicy" not in kinds
    synced = {
        doc["metadata"]["name"]
        for doc in hub_documents
        if doc.get("kind") == "OnePasswordItem"
    }
    assert synced == {"monolith-agents-db", "monolith-r2-s3"}
    deployments = [doc for doc in hub_documents if doc.get("kind") == "Deployment"]
    for deployment in deployments:
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert _secret_refs(container) <= synced


def test_no_inert_cilium_resources(documents: list[dict]) -> None:
    assert documents
    assert not any(
        doc.get("kind") in {"CiliumNetworkPolicy", "CiliumClusterwideNetworkPolicy"}
        for doc in documents
    )
