"""Rendered contract for the on-demand Grimoire migration dump (HOME-484)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent
REPO = CHART.parents[2]
NAME = "grimoire-migration-dump"


def render(*settings: str, jobs_image: bool = True) -> list[dict]:
    """Production values. The build-time jobs image pin is forced by default."""
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "monolith",
        str(CHART),
        "--namespace",
        "monolith",
        "--values",
        os.environ["DEPLOY_VALUES"],
        "--values",
        os.environ["GKE_VALUES"],
    ]
    if jobs_image:
        argv += [
            "--set",
            "jobs.image.repository=registry.invalid/jobs",
            "--set-string",
            "jobs.image.digest=sha256:test",
        ]
    for setting in settings:
        argv += ["--set", setting]
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def find(docs: list[dict], kind: str) -> dict | None:
    return next(
        (
            doc
            for doc in docs
            if doc["kind"] == kind and doc["metadata"]["name"] == NAME
        ),
        None,
    )


def env(container: dict) -> dict[str, dict]:
    return {item["name"]: item for item in container["env"]}


@pytest.fixture(scope="module")
def production() -> list[dict]:
    return render()


def test_production_renders_a_suspended_workflow_that_never_runs_on_schedule(
    production,
):
    workflow = find(production, "CronWorkflow")
    assert workflow is not None
    assert workflow["metadata"]["namespace"] == "monolith-workflows"
    assert workflow["spec"]["suspend"] is True
    assert workflow["spec"]["schedules"] == ["0 0 31 2 *"]
    assert workflow["spec"]["concurrencyPolicy"] == "Forbid"
    spec = workflow["spec"]["workflowSpec"]
    assert spec["podGC"] == {"strategy": "OnWorkflowSuccess"}
    assert spec["securityContext"]["runAsNonRoot"] is True


def test_the_dump_and_the_upload_share_one_pod(production):
    spec = find(production, "CronWorkflow")["spec"]["workflowSpec"]
    (template,) = spec["templates"]
    (dump,) = template["initContainers"]
    upload = template["container"]
    assert dump["command"] == ["bash", "/scripts/dump.sh"]
    assert upload["args"] == ["grimoire-migration-dump"]
    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    assert "emptyDir" in volumes["work"]
    assert volumes["scripts"]["configMap"]["name"] == NAME

    mounts = {
        container["name"]: {m["name"]: m for m in container["volumeMounts"]}
        for container in (dump, {**upload, "name": "upload"})
    }
    assert mounts["pg-dump"]["work"]["mountPath"] == "/work"
    assert not mounts["pg-dump"]["work"].get("readOnly")
    assert mounts["upload"]["work"] == {
        "name": "work",
        "mountPath": "/work",
        "readOnly": True,
    }
    assert "scripts" not in mounts["upload"]
    assert env(dump)["WORK"]["value"] == "/work"
    assert env(upload)["GRIMOIRE_MIGRATION_WORK"]["value"] == "/work"
    # The pod's fsGroup lets the uploader read the files the dump writes.
    assert spec["securityContext"]["fsGroup"] == 65532
    assert dump["securityContext"]["runAsUser"] == 26


def test_pg_dump_matches_the_factory_restore_image(production):
    spec = find(production, "CronWorkflow")["spec"]["workflowSpec"]
    (dump,) = spec["templates"][0]["initContainers"]
    restore = yaml.safe_load(
        (REPO / "projects/grimoire/deploy/restore-cronjob.yaml").read_text()
    )
    pod = restore["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    (pg_restore,) = [c for c in pod["containers"] if c["name"] == "pg-restore"]
    assert dump["image"] == pg_restore["image"]
    assert "@sha256:" in dump["image"]


def test_both_containers_read_the_source_and_only_the_upload_reads_r2(production):
    spec = find(production, "CronWorkflow")["spec"]["workflowSpec"]
    (dump,) = spec["templates"][0]["initContainers"]
    upload = spec["templates"][0]["container"]
    for container in (dump, upload):
        ref = env(container)["DATABASE_URL"]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "monolith-pg-app", "key": "uri"}
    assert env(dump)["GRIMOIRE_SCHEMAS"]["value"] == "grimoire"
    assert not any(name.startswith(("S3_", "AWS_")) for name in env(dump))

    upload_env = env(upload)
    for name in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        ref = upload_env[name]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "monolith-r2-s3"
    assert upload_env["SEAWEEDFS_S3_ENDPOINT"]["value"].startswith("https://")
    assert upload_env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]["value"]


def test_runs_go_to_a_prefix_of_the_private_grimoire_bucket(production):
    upload = find(production, "CronWorkflow")["spec"]["workflowSpec"]["templates"][0][
        "container"
    ]
    upload_env = env(upload)
    assert upload_env["GRIMOIRE_S3_BUCKET"]["value"] == "grimoire"
    assert upload_env["GRIMOIRE_MIGRATION_PREFIX"]["value"] == "migration/"
    # The public buckets the chart writes stay out of this job.
    rendered = yaml.safe_dump(upload)
    for public in ("factory-public", "artifacts", "stars"):
        assert f"value: {public}\n" not in rendered


def test_the_configmap_ships_the_repository_scripts(production):
    data = find(production, "ConfigMap")["data"]
    assert data["dump.sh"] == (CHART / "files/grimoire-migration-dump.sh").read_text()
    assert (
        data["references.sql"] == (CHART / "files/grimoire-references.sql").read_text()
    )
    spec = find(production, "CronWorkflow")["spec"]["workflowSpec"]
    (dump,) = spec["templates"][0]["initContainers"]
    assert env(dump)["REFERENCES_SQL"]["value"] == "/scripts/references.sql"
    assert env(dump)["INVENTORY_SQL"]["value"] == "/scripts/inventory.sql"
    # The source inventory and the factory's target inventory run one query.
    restore_inventory = (REPO / "projects/grimoire/deploy/inventory.sql").read_text()
    assert data["inventory.sql"] == restore_inventory
    assert (CHART / "files/grimoire-inventory.sql").read_text() == restore_inventory


@pytest.mark.parametrize(
    ("settings", "jobs_image"),
    [
        (["grimoire.enabled=false"], True),
        (["sharedS3.enabled=false"], True),
        # A plain render has no jobs image pin, as in cronworkflows.yaml.
        ([], False),
    ],
)
def test_nothing_renders_without_grimoire_shared_s3_or_the_jobs_image(
    settings, jobs_image
):
    docs = render(*settings, jobs_image=jobs_image)
    assert find(docs, "CronWorkflow") is None
    assert find(docs, "ConfigMap") is None
