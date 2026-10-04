"""Invitation rollout flags never provision credentials or bypass login broadly."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent


def render(*settings):
    command = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "kg",
        str(CHART),
        "--set",
        "cfIngress.grimoire.enabled=true",
    ]
    for value in settings:
        command += ["--set", value]
    return subprocess.run(
        command, capture_output=True, text=True, timeout=120, check=False
    )


def documents(result):
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def containers(docs):
    return {
        c["name"]: {e["name"]: e for e in c.get("env", [])}
        for d in docs
        if d["kind"] == "Deployment"
        for c in d["spec"]["template"]["spec"]["containers"]
    }


def test_default_flags_have_no_anonymous_route_or_invitation_secret():
    docs = documents(render())
    assert not any(d["metadata"]["name"].endswith("grimoire-invitation") for d in docs)
    envs = containers(docs)
    for name in ("backend", "frontend"):
        assert envs[name]["GRIMOIRE_INVITATION_LINKS_ENABLED"]["value"] == "false"
        assert "GRIMOIRE_INVITATION_API_TOKEN" not in envs[name]
    assert envs["backend"]["GRIMOIRE_INVITATION_ENROLLMENT_ENABLED"]["value"] == "false"


def test_only_exact_landing_is_anonymous_and_existing_policy_unchanged():
    baseline = documents(render())
    enabled = documents(render("grimoire.invitationLinks.enabled=true"))
    policies = lambda docs: [d for d in docs if d["kind"] == "SecurityPolicy"]
    assert policies(enabled) == policies(baseline)
    route = next(
        d
        for d in enabled
        if d["kind"] == "HTTPRoute"
        and d["metadata"]["name"].endswith("grimoire-invitation")
    )
    assert route["spec"]["rules"][0]["matches"] == [
        {"path": {"type": "Exact", "value": "/grimoire/join"}}
    ]
    assert len(route["spec"]["rules"]) == 1
    assert any(
        d["kind"] == "BackendTrafficPolicy"
        and d["metadata"]["name"] == route["metadata"]["name"]
        for d in enabled
    )


@pytest.mark.parametrize(
    "settings",
    [
        ("grimoire.invitationLinks.enrollmentEnabled=true",),
        (
            "grimoire.invitationLinks.enabled=true",
            "grimoire.invitationLinks.enrollmentEnabled=true",
        ),
        (
            "grimoire.invitationLinks.enabled=true",
            "grimoire.invitationLinks.enrollmentEnabled=true",
            "grimoire.invitationLinks.existingSecretName=reviewed-secret",
        ),
    ],
)
def test_enrollment_requires_explicit_existing_secret_and_flow(settings):
    assert render(*settings).returncode != 0


def test_enrollment_secret_only_referenced_in_backend_and_no_secret_provisioned():
    settings = (
        "grimoire.invitationLinks.enabled=true",
        "grimoire.invitationLinks.enrollmentEnabled=true",
        "grimoire.invitationLinks.existingSecretName=reviewed-secret",
        "grimoire.invitationLinks.flowID=22222222-2222-4222-8222-222222222222",
    )
    docs = documents(render(*settings))
    envs = containers(docs)
    assert envs["backend"]["GRIMOIRE_INVITATION_API_TOKEN"]["valueFrom"][
        "secretKeyRef"
    ] == {"name": "reviewed-secret", "key": "api-token"}
    assert "GRIMOIRE_INVITATION_API_TOKEN" not in envs["frontend"]
    assert not any(
        d["kind"] in ("Secret", "OnePasswordItem")
        and d["metadata"]["name"] == "reviewed-secret"
        for d in docs
    )
