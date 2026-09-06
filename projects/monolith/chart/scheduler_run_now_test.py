"""Render assertions for scheduler run-now Argo integration."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml


def _render(settings=()) -> list[dict]:
    chart_dir = Path(__file__).resolve().parent
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(chart_dir),
            "--namespace",
            "monolith",
            "--set",
            "jobs.image.repository=registry.invalid/jobs",
            "--set-string",
            "jobs.image.digest=sha256:test",
            *[argument for setting in settings for argument in ("--set", setting)],
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


@pytest.fixture(scope="module")
def rendered_documents() -> list[dict]:
    return _render()


def _resource(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        doc
        for doc in documents
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name
    )


def test_replacing_cronworkflow_has_scheduler_annotation(rendered_documents):
    workflow = _resource(rendered_documents, "CronWorkflow", "worldcup-sim")
    assert workflow["metadata"]["annotations"] == {
        "monolith.jomcgi.dev/replaces": "worldcup.refresh"
    }


def test_scheduler_workflow_role_has_exact_argo_verbs(rendered_documents):
    role = _resource(rendered_documents, "Role", "monolith-scheduler-workflows")
    assert role["metadata"]["namespace"] == "monolith-workflows"
    assert role["rules"] == [
        {
            "apiGroups": ["argoproj.io"],
            "resources": ["cronworkflows"],
            "verbs": ["list"],
        },
        {
            "apiGroups": ["argoproj.io"],
            "resources": ["workflows"],
            "verbs": ["create"],
        },
    ]


def test_scheduler_workflow_rolebinding_grants_monolith_serviceaccount(
    rendered_documents,
):
    binding = _resource(
        rendered_documents, "RoleBinding", "monolith-scheduler-workflows"
    )
    assert binding["metadata"]["namespace"] == "monolith-workflows"
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "monolith-scheduler-workflows",
    }
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "monolith",
            "namespace": "monolith",
        }
    ]


def test_scheduler_workflow_namespace_is_rendered_into_backend_env(
    rendered_documents,
):
    deployment = _resource(rendered_documents, "Deployment", "monolith")
    backend = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "backend"
    )
    env = {item["name"]: item.get("value") for item in backend["env"]}
    assert env["SCHEDULER_WORKFLOW_NAMESPACE"] == "monolith-workflows"


def test_scheduler_grant_can_be_disabled_without_changing_workflow_resources():
    documents = _render(["rbac.schedulerWorkflows.enabled=false"])
    assert not any(
        doc["kind"] in {"Role", "RoleBinding"}
        and doc["metadata"]["name"] == "monolith-scheduler-workflows"
        for doc in documents
    )
    assert _resource(documents, "CronWorkflow", "worldcup-sim")
    test_scheduler_workflow_namespace_is_rendered_into_backend_env(documents)


def test_scheduler_grant_and_client_follow_configured_namespace():
    documents = _render(["jobs.workflowNamespace=recovery-workflows"])
    for kind in ("Role", "RoleBinding"):
        resource = _resource(documents, kind, "monolith-scheduler-workflows")
        assert resource["metadata"]["namespace"] == "recovery-workflows"
    binding = _resource(documents, "RoleBinding", "monolith-scheduler-workflows")
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "monolith", "namespace": "monolith"}
    ]
    deployment = _resource(documents, "Deployment", "monolith")
    backend = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "backend"
    )
    env = {item["name"]: item.get("value") for item in backend["env"]}
    assert env["SCHEDULER_WORKFLOW_NAMESPACE"] == "recovery-workflows"
