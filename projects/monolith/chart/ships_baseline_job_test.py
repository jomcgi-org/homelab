"""HOME-483: the ships-baseline CronWorkflow, its RBAC and its private R2 key."""

import os
import subprocess
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parent


def render(*extra):
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(CHART),
            "--namespace",
            "monolith",
            "--set",
            "jobs.image.repository=registry.invalid/jobs",
            "--set-string",
            "jobs.image.digest=sha256:test",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def find(docs, kind, name):
    return next(
        doc
        for doc in docs
        if doc.get("kind") == kind and doc["metadata"]["name"] == name
    )


def names(docs):
    return {(doc.get("kind"), doc["metadata"]["name"]) for doc in docs}


def test_schedule_retry_and_identity():
    job = find(render(), "CronWorkflow", "ships-baseline")
    spec = job["spec"]
    assert job["metadata"]["namespace"] == "monolith-workflows"
    assert spec["schedules"] == ["0 0 * * *"]
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["suspend"] is False
    workflow = spec["workflowSpec"]
    # The 24-hour window, retries and the final count fit in 26 hours.
    assert workflow["activeDeadlineSeconds"] == 93600
    assert workflow["serviceAccountName"] == "ships-baseline"
    template = workflow["templates"][0]
    assert template["retryStrategy"]["retryPolicy"] == "Always"
    assert template["retryStrategy"]["limit"] == 5
    container = template["container"]
    assert container["args"] == ["ships-baseline"]
    assert container["resources"]["limits"]["memory"] == "512Mi"


def test_env_points_at_the_backend_pods_and_the_private_bucket():
    docs = render()
    container = find(docs, "CronWorkflow", "ships-baseline")["spec"]["workflowSpec"][
        "templates"
    ][0]["container"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["SHIPS_BASELINE_RUN"]["value"] == "{{workflow.name}}"
    assert env["SHIPS_BASELINE_S3_BUCKET"]["value"] == "stars"
    assert env["SHIPS_BASELINE_S3_PREFIX"]["value"] == "ships-baseline/"
    assert env["SHIPS_BASELINE_WINDOW_MINUTES"]["value"] == "1440"
    assert env["SHIPS_BASELINE_NAMESPACE"]["value"] == "monolith"
    assert env["SEAWEEDFS_S3_ENDPOINT"]["value"].startswith("https://")
    for key in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        assert env[key]["valueFrom"]["secretKeyRef"]["name"] == "monolith-r2-s3"
    assert "DATABASE_URL" in env

    # The selector and container name match the backend Deployment.
    deployment = find(docs, "Deployment", "monolith")
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    selector = dict(
        pair.split("=", 1)
        for pair in env["SHIPS_BASELINE_POD_SELECTOR"]["value"].split(",")
    )
    assert selector.items() <= labels.items()
    assert selector["app.kubernetes.io/component"] == "app"
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert "backend" in {c["name"] for c in containers}


def test_rbac_reads_only_the_release_namespace():
    docs = render()
    account = find(docs, "ServiceAccount", "ships-baseline")
    assert account["metadata"]["namespace"] == "monolith-workflows"
    read = find(docs, "Role", "ships-baseline-read")
    assert read["metadata"]["namespace"] == "monolith"
    assert read["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        {"apiGroups": ["metrics.k8s.io"], "resources": ["pods"], "verbs": ["list"]},
    ]
    executor = find(docs, "Role", "ships-baseline-executor")
    assert executor["metadata"]["namespace"] == "monolith-workflows"
    assert executor["rules"] == [
        {
            "apiGroups": ["argoproj.io"],
            "resources": ["workflowtaskresults"],
            "verbs": ["create", "patch"],
        }
    ]
    subject = {
        "kind": "ServiceAccount",
        "name": "ships-baseline",
        "namespace": "monolith-workflows",
    }
    for name, namespace in (
        ("ships-baseline-read", "monolith"),
        ("ships-baseline-executor", "monolith-workflows"),
    ):
        binding = find(docs, "RoleBinding", name)
        assert binding["metadata"]["namespace"] == namespace
        assert binding["roleRef"] == {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": name,
        }
        assert binding["subjects"] == [subject]
    assert not any(
        doc.get("kind") in {"ClusterRole", "ClusterRoleBinding"}
        and "ships-baseline" in doc["metadata"]["name"]
        for doc in docs
    )


def test_job_and_rbac_need_shared_s3():
    rendered = names(render("--set", "sharedS3.enabled=false"))
    assert ("CronWorkflow", "ships-baseline") not in rendered
    assert ("ServiceAccount", "ships-baseline") not in rendered
    assert ("Role", "ships-baseline-read") not in rendered


def test_job_and_rbac_need_the_jobs_image():
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(CHART),
            "--namespace",
            "monolith",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    docs = [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]
    rendered = names(docs)
    assert ("CronWorkflow", "ships-baseline") not in rendered
    assert ("RoleBinding", "ships-baseline-read") not in rendered
