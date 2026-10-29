"""Rendering a deployment must not opt in to knowledge mutation jobs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent
DEPLOY = Path(os.environ.get("DEPLOY_VALUES", CHART.parent / "deploy/values.yaml"))
GKE = Path(os.environ.get("GKE_VALUES", CHART.parent / "deploy/values-gke.yaml"))


REPLACES_ANNOTATION = "monolith.jomcgi.dev/replaces"
PILOT_REPLACES = {
    "knowledge-review-backfill-dry-run": "knowledge.review_backfill_dry_run",
    "knowledge-review-backfill-pilot": "knowledge.review_backfill_pilot",
    "knowledge-review-admission-dry-run": "knowledge.review_admission_dry_run",
}
APPLY_JOBS = ("knowledge-review-backfill", "knowledge-review-admission")


def _render_documents(*values: Path) -> list[dict]:
    command = [
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
    ]
    for path in values:
        command.extend(["--values", str(path)])
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _cronworkflows(documents: list[dict]) -> dict[str, dict]:
    """Full CronWorkflow documents (metadata included) by name."""
    return {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc.get("kind") == "CronWorkflow"
    }


def _render(*values: Path) -> dict[str, dict]:
    """CronWorkflow specs by name."""
    return {
        name: doc["spec"]
        for name, doc in _cronworkflows(_render_documents(*values)).items()
    }


def _backend_env(documents: list[dict]) -> dict[str, str | None]:
    deployment = next(
        doc
        for doc in documents
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "monolith"
    )
    backend = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "backend"
    )
    return {item["name"]: item.get("value") for item in backend["env"]}


@pytest.mark.parametrize("values", [(), (DEPLOY,), (DEPLOY, GKE)])
def test_source_coverage_defaults_and_production_opt_in(values, tmp_path):
    before = _render_documents(*values)
    env = _backend_env(before)
    expected = "true" if DEPLOY in values else "false"
    assert env.get("KG_FACT_REVIEW_COVERAGE_ENABLED", "false") == expected
    domain = tmp_path / "knowledge.yaml"
    domain.write_text(
        "knowledge:\n  enabled: true\n  factReview:\n    coverageEnabled: false\n"
    )
    before = _render_documents(*values, domain)
    env = _backend_env(before)
    assert env["KG_FACT_REVIEW_COVERAGE_ENABLED"] == "false"
    override = tmp_path / "coverage.yaml"
    override.write_text("knowledge:\n  factReview:\n    coverageEnabled: true\n")
    after = _render_documents(*values, domain, override)
    assert _backend_env(after) == env | {"KG_FACT_REVIEW_COVERAGE_ENABLED": "true"}
    assert _cronworkflows(before) == _cronworkflows(after)


@pytest.mark.parametrize("values", [(), (DEPLOY,), (DEPLOY, GKE)])
def test_review_mutation_jobs_are_suspended_by_default(values):
    jobs = _render(*values)
    # Force the jobs image above so a missing CronWorkflow cannot pass vacuously.
    for name in ("knowledge-review-backfill", "knowledge-review-admission"):
        assert jobs[name]["suspend"] is True
    backfill = jobs["knowledge-review-backfill"]
    assert backfill["schedules"] == ["*/5 * * * *"]
    assert backfill["concurrencyPolicy"] == "Forbid"
    workflow = backfill["workflowSpec"]
    assert workflow["activeDeadlineSeconds"] == 300
    assert workflow["templates"][0]["container"]["args"] == [
        "knowledge-review-backfill",
        "--apply",
        "--pending-only",
    ]


def test_play_embedding_cronworkflow_is_bounded_and_grimoire_gated(tmp_path):
    entries = yaml.safe_load((CHART / "values.yaml").read_text())["jobs"][
        "cronWorkflows"
    ]
    entry = next(job for job in entries if job["name"] == "grimoire-embed-play")
    assert entry["grimoire"] is True
    for enabled in (False, True):
        override = tmp_path / "play.yaml"
        override.write_text(yaml.safe_dump({"grimoire": {"enabled": enabled}}))
        jobs = _render(override)
        if not enabled:
            assert "grimoire-embed-play" not in jobs
            continue
        job = jobs["grimoire-embed-play"]
        assert job["suspend"] is False
        assert job["schedules"] == ["*/5 * * * *"]
        assert job["concurrencyPolicy"] == "Forbid"
        workflow = job["workflowSpec"]
        assert workflow["activeDeadlineSeconds"] == 240
        container = workflow["templates"][0]["container"]
        assert container["args"] == ["grimoire-embed-play"]
        assert container["resources"] == {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"memory": "256Mi"},
        }
        assert "EMBEDDING_URL" in {item["name"] for item in container["env"]}


def test_backfill_requires_explicit_opt_in_without_enabling_admission(tmp_path):
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    entries = values["jobs"]["cronWorkflows"]
    backfill = next(
        job for job in entries if job["name"] == "knowledge-review-backfill"
    )
    assert backfill["suspend"] is True
    backfill["suspend"] = False
    override = tmp_path / "approved-backfill.yaml"
    # Helm replaces arrays. Preserve every other job's settings in this override.
    override.write_text(yaml.safe_dump({"jobs": {"cronWorkflows": entries}}))
    jobs = _render(DEPLOY, GKE, override)
    assert jobs["knowledge-review-backfill"]["suspend"] is False
    assert jobs["knowledge-review-admission"]["suspend"] is True


@pytest.mark.parametrize("values", [(), (DEPLOY,), (DEPLOY, GKE)])
def test_review_pilot_jobs_require_manual_submission_and_preserve_bounds(values):
    cronworkflows = _cronworkflows(_render_documents(*values))
    jobs = {name: doc["spec"] for name, doc in cronworkflows.items()}
    expected = {
        "knowledge-review-backfill-dry-run": [
            "knowledge-review-backfill",
            "--pending-only",
            "--batch-size",
            "20",
            "--max-batches",
            "1",
        ],
        "knowledge-review-backfill-pilot": [
            "knowledge-review-backfill",
            "--apply",
            "--pending-only",
            "--batch-size",
            "20",
            "--max-batches",
            "1",
        ],
        "knowledge-review-admission-dry-run": [
            "knowledge-review-admission",
            "--batch-size",
            "20",
            "--max-requests",
            "60",
            "--deadline-seconds",
            "240",
        ],
    }
    for name, args in expected.items():
        job = jobs[name]
        assert job["suspend"] is True
        assert job["concurrencyPolicy"] == "Forbid"
        workflow = job["workflowSpec"]
        assert workflow["activeDeadlineSeconds"] == 240
        container = workflow["templates"][0]["container"]
        assert container["args"] == args
        assert "GITHUB_TOKEN" not in {item["name"] for item in container["env"]}
        # replaces puts the pilot in ARGO_JOBS, which is what lets the scheduler
        # run-now path submit it one-off while it stays suspended.
        annotations = cronworkflows[name]["metadata"]["annotations"]
        assert annotations[REPLACES_ANNOTATION] == PILOT_REPLACES[name]
    for name in APPLY_JOBS:
        # The apply jobs must not be submittable through run-now: GitOps
        # suspend stays their only switch.
        metadata = cronworkflows[name]["metadata"]
        assert REPLACES_ANNOTATION not in (metadata.get("annotations") or {})


@pytest.mark.parametrize("values", [(), (DEPLOY,), (DEPLOY, GKE)])
def test_argo_jobs_lists_pilot_jobs_but_not_apply_jobs(values):
    documents = _render_documents(*values)
    argo_jobs = set(filter(None, _backend_env(documents)["ARGO_JOBS"].split(",")))
    assert set(PILOT_REPLACES.values()) <= argo_jobs
    assert not argo_jobs & set(APPLY_JOBS)
    assert not argo_jobs & {"knowledge.review_backfill", "knowledge.review_admission"}
    # No knowledge-review name reaches ARGO_JOBS except through the pilot replaces.
    assert {n for n in argo_jobs if "review" in n} == set(PILOT_REPLACES.values())


@pytest.mark.parametrize("values", [(), (DEPLOY,), (DEPLOY, GKE)])
def test_all_review_workflows_serialize_and_capture_receipts(values):
    jobs = _render(*values)
    names = [name for name in jobs if name.startswith("knowledge-review-")]
    assert len(names) == 5
    for name in names:
        workflow = jobs[name]["workflowSpec"]
        assert jobs[name]["suspend"] is True
        assert workflow["synchronization"] == {
            "mutexes": [{"name": "knowledge-review-pilot"}]
        }
        assert workflow["templates"][0]["outputs"] == {
            "parameters": [
                {
                    "name": "review-report",
                    "valueFrom": {"path": "/tmp/knowledge-review-report.json"},
                }
            ]
        }


@pytest.mark.parametrize("values", [(DEPLOY,), (DEPLOY, GKE)])
def test_grimoire_workflows_reference_existing_s3_secret(values):
    entries = yaml.safe_load((CHART / "values.yaml").read_text())["jobs"][
        "cronWorkflows"
    ]
    jobs = _render(*values)
    for entry in entries:
        if not entry.get("grimoire"):
            continue
        container = jobs[entry["name"]]["workflowSpec"]["templates"][0]["container"]
        env = {item["name"]: item for item in container["env"]}
        for name, key in (
            ("S3_ACCESS_KEY_ID", "access-key-id"),
            ("S3_SECRET_ACCESS_KEY", "secret-access-key"),
        ):
            assert env[name]["valueFrom"]["secretKeyRef"] == {
                "name": "monolith-r2-s3",
                "key": key,
            }


@pytest.mark.parametrize("values", [(), (DEPLOY,), (DEPLOY, GKE)])
def test_completed_raw_recovery_opt_in_does_not_schedule_work(values, tmp_path):
    override = tmp_path / "recovery.yaml"
    override.write_text("knowledge:\n  enabled: true\n")
    before = _render_documents(*values, override)
    env = _backend_env(before)
    assert env["KG_COMPLETED_RAW_RECOVERY_ENABLED"] == "false"
    override.write_text(
        "knowledge:\n  enabled: true\n  completedRawRecoveryEnabled: true\n"
    )
    after = _render_documents(*values, override)
    assert _backend_env(after) == env | {"KG_COMPLETED_RAW_RECOVERY_ENABLED": "true"}
    assert _cronworkflows(before) == _cronworkflows(after)


@pytest.mark.parametrize("values", [DEPLOY, GKE])
def test_learning_handoff_is_explicit_and_does_not_add_scheduler(values, tmp_path):
    baseline = _render_documents(values)
    override = tmp_path / "learning.yaml"
    override.write_text("agents:\n  drainer:\n    learningHandoffEnabled: true\n")
    opted = _render_documents(values, override)

    def flags(documents):
        return [
            env["value"]
            for doc in documents
            if doc.get("kind") == "Deployment"
            for container in doc["spec"]["template"]["spec"]["containers"]
            for env in container.get("env", [])
            if env.get("name") == "FACTORY_LEARNING_HANDOFF_ENABLED"
        ]

    assert flags(baseline) and set(flags(baseline)) == {
        "true" if values == DEPLOY else "false"
    }
    assert set(flags(opted)) == {"true"}
    assert _cronworkflows(baseline) == _cronworkflows(opted)


def test_production_learning_activation_preserves_all_other_controls(tmp_path):
    before_values = tmp_path / "before.yaml"
    before_values.write_text(
        "knowledge:\n  factReview:\n    coverageEnabled: false\nagents:\n  drainer:\n    learningHandoffEnabled: false\n"
    )
    before = _render_documents(DEPLOY, GKE, before_values)
    after = _render_documents(DEPLOY, GKE)
    env = _backend_env(after)
    assert env == _backend_env(before) | {
        "KG_FACT_REVIEW_COVERAGE_ENABLED": "true",
        "FACTORY_LEARNING_HANDOFF_ENABLED": "true",
    }
    for key in (
        "KG_COMPLETED_RAW_RECOVERY_ENABLED",
        "KG_AUDIT_CLARITY_REPAIRS_ENABLED",
        "KG_AUDIT_ISSUES_ENABLED",
    ):
        assert env[key] == "false"
    assert env["KG_FACT_REVIEW_MAX_DAILY"] == "20000"
    assert _cronworkflows(before) == _cronworkflows(after)


def test_daily_learning_hash_is_empty_until_explicitly_pinned(tmp_path):
    before = _render_documents(DEPLOY, GKE)
    env = _backend_env(before)
    assert env["FACTORY_LEARNING_DAILY_PAYLOAD_SHA256"] == ""
    override = tmp_path / "pin.yaml"
    override.write_text(
        'agents:\n  drainer:\n    learningDailyPayloadSha256: "' + "a" * 64 + '"\n'
    )
    after = _render_documents(DEPLOY, GKE, override)
    assert _backend_env(after) == env | {
        "FACTORY_LEARNING_DAILY_PAYLOAD_SHA256": "a" * 64
    }
    assert _cronworkflows(before) == _cronworkflows(after)
