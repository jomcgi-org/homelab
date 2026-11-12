"""Verify default-off, production-on and rollback Grimoire flags through Helm."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("layer", ["chart", "deploy", "gke"])
def test_creation_restriction_is_default_off_and_only_changes_backend_env(layer):
    chart = Path(__file__).resolve().parent
    command = [os.environ.get("HELM_BIN", "helm"), "template", "kg", str(chart)]
    if layer in ("deploy", "gke"):
        command += [
            "-f",
            os.environ.get("DEPLOY_VALUES", str(chart.parent / "deploy/values.yaml")),
        ]
    if layer == "gke":
        command += [
            "-f",
            os.environ.get("GKE_VALUES", str(chart.parent / "deploy/values-gke.yaml")),
        ]

    def render(*settings):
        result = subprocess.run(
            command + list(settings),
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def creation_env(docs):
        backend = next(
            container
            for doc in docs
            if doc["kind"] == "Deployment"
            for container in doc["spec"]["template"]["spec"]["containers"]
            if container["name"] == "backend"
        )
        return {
            entry["name"]: entry["value"]
            for entry in backend["env"]
            if entry["name"]
            in ("GRIMOIRE_CREATION_RESTRICTION_ENABLED", "GRIMOIRE_CREATOR_EMAILS")
        }

    defaults = render()
    assert creation_env(defaults) == {
        "GRIMOIRE_CREATION_RESTRICTION_ENABLED": "false",
        "GRIMOIRE_CREATOR_EMAILS": "",
    }
    enabled_empty = render("--set", "grimoire.campaignCreation.restrictionEnabled=true")
    assert creation_env(enabled_empty) == {
        "GRIMOIRE_CREATION_RESTRICTION_ENABLED": "true",
        "GRIMOIRE_CREATOR_EMAILS": "",
    }
    configured = render(
        "--set",
        "grimoire.campaignCreation.restrictionEnabled=true",
        "--set",
        "grimoire.campaignCreation.allowedEmails[0]=owner@example.test",
        "--set",
        "grimoire.campaignCreation.allowedEmails[1]=dm@example.test",
    )
    assert creation_env(configured) == {
        "GRIMOIRE_CREATION_RESTRICTION_ENABLED": "true",
        "GRIMOIRE_CREATOR_EMAILS": "owner@example.test,dm@example.test",
    }
    # Strip only the two permitted env entries; every resource, including
    # Secrets, OnePasswordItems, auth routes and other feature flags, must match.
    for docs in (defaults, enabled_empty, configured):
        for doc in docs:
            if doc["kind"] == "Deployment":
                for container in doc["spec"]["template"]["spec"]["containers"]:
                    container["env"] = [
                        entry
                        for entry in container.get("env", [])
                        if entry["name"]
                        not in (
                            "GRIMOIRE_CREATION_RESTRICTION_ENABLED",
                            "GRIMOIRE_CREATOR_EMAILS",
                        )
                    ]
    assert defaults == enabled_empty == configured


@pytest.mark.parametrize("deploy", (False, True))
@pytest.mark.parametrize(
    "enabled,expected", ((None, "false"), (True, "true"), (False, "false"))
)
def test_transcript_default_off_and_explicit_switch_reach_both_containers(
    deploy, enabled, expected
):
    chart = Path(__file__).resolve().parent
    command = [os.environ.get("HELM_BIN", "helm"), "template", "kg", str(chart)]
    if deploy:
        command.extend(
            [
                "-f",
                os.environ.get(
                    "DEPLOY_VALUES", str(chart.parent / "deploy" / "values.yaml")
                ),
            ]
        )
    if enabled is not None:
        command.extend(["--set", f"grimoire.transcript.enabled={str(enabled).lower()}"])
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=120, check=True
    )
    containers = {
        container["name"]: container
        for doc in yaml.safe_load_all(result.stdout)
        if doc and doc.get("kind") == "Deployment"
        for container in doc["spec"]["template"]["spec"]["containers"]
        if container["name"] in ("backend", "frontend")
    }
    assert set(containers) == {"backend", "frontend"}
    for container in containers.values():
        values = [
            entry["value"]
            for entry in container["env"]
            if entry["name"] == "GRIMOIRE_TRANSCRIPT_ENABLED"
        ]
        assert values == [expected]
        assert isinstance(values[0], str)


@pytest.mark.parametrize("deploy", [False, True])
@pytest.mark.parametrize("enabled", [None, True, False])
def test_factory_async_residency_default_off_and_explicit_rollback(deploy, enabled):
    chart = Path(__file__).resolve().parent
    command = [os.environ.get("HELM_BIN", "helm"), "template", "kg", str(chart)]
    if deploy:
        command += [
            "-f",
            os.environ.get(
                "DEPLOY_VALUES", str(chart.parent / "deploy" / "values.yaml")
            ),
        ]
    if enabled is not None:
        command += [
            "--set",
            f"agents.sessions.asyncResidencyEnabled={str(enabled).lower()}",
        ]
    rendered = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=120
    )
    values = [
        entry["value"]
        for doc in yaml.safe_load_all(rendered.stdout)
        if doc and doc.get("kind") == "Deployment"
        for container in doc["spec"]["template"]["spec"]["containers"]
        for entry in container.get("env", [])
        if entry["name"] == "FACTORY_ASYNC_RESIDENCY_ENABLED"
    ]
    assert values
    assert set(values) == {"true" if enabled else "false"}
