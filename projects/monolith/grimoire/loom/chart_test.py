"""Values and actual Helm render guards for dormant Loom wiring."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_default_and_deployment_loom_off():
    for path in (ROOT / "chart/values.yaml", ROOT / "deploy/values.yaml"):
        values = yaml.safe_load(path.read_text())
        assert values["grimoire"]["loom"]["enabled"] is False
        assert (
            values["grimoire"]["loom"]["baseUrl"]
            == "http://loom-query-api.loom.svc:8080"
        )
    values = yaml.safe_load((ROOT / "chart/values.yaml").read_text())
    job = next(
        v
        for v in values["jobs"]["cronWorkflows"]
        if v["name"] == "grimoire-loom-ontology"
    )
    assert job["suspend"] is True
    assert job["loomOntology"] is True
    assert job["concurrencyPolicy"] == "Forbid"
    assert not values["grimoire"]["loom"]["adminExistingSecret"]


def render(*args):
    import os
    import subprocess

    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(ROOT / "chart"),
            "--set",
            "jobs.image.repository=example.invalid/jobs",
            "--set",
            "jobs.image.tag=test",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout, [d for d in yaml.safe_load_all(result.stdout) if d]


def test_flag_off_renders_no_loom_resources_or_configuration():
    for args in ((), ("-f", str(ROOT / "deploy/values.yaml"))):
        text, docs = render(*args)
        assert "loom-grimoire-token" not in text
        assert "GRIMOIRE_LOOM" not in text
        assert not any(d["metadata"]["name"] == "grimoire-loom-ontology" for d in docs)
        assert "grimoire.loom_ontology" not in text


def test_conditional_manual_job_and_separate_credentials():
    text, docs = render(
        "--set", "grimoire.enabled=true", "--set", "grimoire.loom.enabled=true"
    )
    job = next(d for d in docs if d["metadata"]["name"] == "grimoire-loom-ontology")
    assert job["kind"] == "CronWorkflow"
    assert job["spec"]["suspend"] is True
    assert job["spec"]["concurrencyPolicy"] == "Forbid"
    assert (
        job["metadata"]["annotations"]["monolith.jomcgi.dev/replaces"]
        == "grimoire.loom_ontology"
    )
    assert "GRIMOIRE_LOOM_ADMIN_TOKEN" not in str(job)
    assert "loom-grimoire-token" not in str(job)
    secret = next(
        d
        for d in docs
        if d["kind"] == "OnePasswordItem"
        and d["metadata"]["name"].endswith("-loom-grimoire-token")
    )
    assert secret["spec"]["itemPath"] == "vaults/k8s-homelab/items/loom-grimoire-token"
    assert "GRIMOIRE_LOOM_ADMIN_TOKEN" not in text
    text, docs = render(
        "--set",
        "grimoire.enabled=true",
        "--set",
        "grimoire.loom.enabled=true",
        "--set",
        "grimoire.loom.adminExistingSecret=dummy-future-admin",
    )
    job = next(d for d in docs if d["metadata"]["name"] == "grimoire-loom-ontology")
    assert "dummy-future-admin" in str(job)
    for d in docs:
        if d["kind"] == "Deployment":
            assert "GRIMOIRE_LOOM_ADMIN_TOKEN" not in str(d)
    text, _ = render(
        "--set", "grimoire.enabled=false", "--set", "grimoire.loom.enabled=true"
    )
    assert "loom-grimoire-token" not in text
    assert "GRIMOIRE_LOOM" not in text
