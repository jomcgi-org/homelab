"""UKV execution stays absent in defaults and every monolith overlay."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent
DOMAIN = CHART.parent
VALUES = [
    DOMAIN / "deploy/values.yaml",
    DOMAIN / "deploy/values-gke.yaml",
    DOMAIN / "dev/deploy/values.yaml",
    DOMAIN / "dev/deploy/values-recovery-gke.yaml",
]


def render(values, *sets):
    dev = any("dev" in p.parts for p in values)
    release = "monolith-dev" if dev else "monolith"
    command = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        release,
        str(CHART),
        "--namespace",
        release,
    ]
    if not dev:
        command += [
            "--set-json",
            'jobs.image={"repository":"test.invalid/jobs","digest":"sha256:'
            + "0" * 64
            + '"}',
        ]
    for path in values:
        command += ["-f", str(path)]
    for value in sets:
        command += ["--set", value]
    output = subprocess.run(command, check=True, capture_output=True, text=True)
    return [d for d in yaml.safe_load_all(output.stdout) if d]


@pytest.mark.parametrize(
    "values", [[], [VALUES[0]], VALUES[:2], [VALUES[0], VALUES[2]], [VALUES[3]]]
)
def test_ukv_absent_and_execution_disabled(values):
    for path in values:
        assert yaml.safe_load(path.read_text())["ukv"]["producerEnabled"] is False
    docs = render(values)
    assert not any(
        d["kind"] == "CronWorkflow" and d["metadata"]["name"] == "hikes-ukv-materialize"
        for d in docs
    )
    # Ordinary jobs cannot accidentally enable the command through inherited env.
    for d in docs:
        if d["kind"] != "CronWorkflow":
            continue
        env = {
            e["name"]: e.get("value")
            for e in d["spec"]["workflowSpec"]["templates"][0]["container"]["env"]
        }
        assert env.get("UKV_PRODUCER_ENABLED", "false") == "false"


def test_explicit_opt_in_stays_suspended_and_bounded():
    docs = render([], "ukv.producerEnabled=true")
    job = next(
        d
        for d in docs
        if d["kind"] == "CronWorkflow"
        and d["metadata"]["name"] == "hikes-ukv-materialize"
    )
    assert job["spec"]["suspend"] is True
    assert job["spec"]["concurrencyPolicy"] == "Forbid"
    spec = job["spec"]["workflowSpec"]
    assert spec["activeDeadlineSeconds"] == 300
    container = spec["templates"][0]["container"]
    assert container["args"] == ["hikes-ukv-materialize"]
    assert container["resources"]["limits"]["memory"] == "512Mi"
    env = {e["name"]: e for e in container["env"]}
    assert env["UKV_PRODUCER_ENABLED"]["value"] == "true"
    assert (
        "UKV_SAMPLED_INPUT" not in env
    )  # no live acquisition adapter or input mounted


def test_string_flag_is_rejected():
    with pytest.raises(subprocess.CalledProcessError):
        render([], "ukv.producerEnabled=typo")
