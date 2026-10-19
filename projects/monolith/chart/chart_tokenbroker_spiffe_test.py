"""Render checks for the monolith token broker SPIFFE client."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml


def _chart_dir() -> Path:
    chart = Path(__file__).resolve().parent
    if not (chart / "Chart.yaml").exists():
        raise RuntimeError("Could not find chart Chart.yaml")
    return chart


def _render(release: str, settings: list[str] | None = None) -> list[dict]:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    argv = [
        helm_bin,
        "template",
        release,
        str(_chart_dir()),
        "--namespace",
        release,
    ]
    for setting in settings or []:
        argv += ["--set", setting]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _object(objects: list[dict], kind: str, name: str) -> dict:
    matches = [
        obj
        for obj in objects
        if obj.get("kind") == kind and obj.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, (kind, name, len(matches))
    return matches[0]


def _container(deployment: dict, name: str) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return next(container for container in containers if container["name"] == name)


def _env(container: dict) -> dict[str, str]:
    return {
        item["name"]: str(item["value"])
        for item in container.get("env", [])
        if "value" in item
    }


def _file_mode(config: str, name: str) -> int:
    matches = re.findall(
        rf"^[ \t]*{name}[ \t]*=[ \t]*(.*?)[ \t]*$", config, re.MULTILINE
    )
    assert len(matches) == 1, f"expected exactly one {name}: {matches}"
    assert re.fullmatch(r"0[0-7]{3}", matches[0]), f"invalid {name}: {matches[0]}"
    return int(matches[0], 8)


def test_file_mode_parser_rejects_missing_malformed_and_duplicate_modes() -> None:
    for config in [
        "",
        "key_file_mode = ",
        "key_file_mode = 0648",
        "key_file_mode = 644",
        'key_file_mode = "0640"',
        "key_file_mode = 0640\nkey_file_mode = 0640",
    ]:
        try:
            _file_mode(config, "key_file_mode")
        except AssertionError:
            pass
        else:
            raise AssertionError(f"accepted invalid file mode: {config!r}")


def test_default_render_preserves_plaintext_and_omits_spiffe_runtime() -> None:
    objects = _render("monolith")
    deployment = _object(objects, "Deployment", "monolith")
    backend = _container(deployment, "backend")

    assert _env(backend)["EMBER_TOKENBROKER_URL"].startswith("http://")
    assert _env(backend)["EMBER_TOKENBROKER_SPIFFE_ENABLED"] == "false"
    assert not any(
        container["name"] == "tokenbroker-spiffe-helper"
        for container in deployment["spec"]["template"]["spec"]["containers"]
    )
    assert not any(
        obj.get("metadata", {}).get("name") == "monolith-tokenbroker-spiffe-helper"
        for obj in objects
    )


def test_enabled_render_packages_helper_svid_and_exact_server_identity() -> None:
    objects = _render(
        "monolith",
        [
            "tokenBroker.spiffe.enabled=true",
        ],
    )
    deployment = _object(objects, "Deployment", "monolith")
    pod = deployment["spec"]["template"]["spec"]
    backend = _container(deployment, "backend")
    helper = _container(deployment, "tokenbroker-spiffe-helper")
    env = _env(backend)

    assert env["EMBER_TOKENBROKER_URL"] == (
        "https://embervm-embervm-tokenbroker.embervm.svc:8443"
    )
    assert env["EMBER_TOKENBROKER_SPIFFE_ENABLED"] == "true"
    assert env["EMBER_TOKENBROKER_SPIFFE_ID"] == (
        "spiffe://embervm.jomcgi.dev/ns/embervm/sa/embervm-embervm-tokenbroker"
    )
    assert helper["image"] == "ghcr.io/spiffe/spiffe-helper:0.11.0"
    assert helper["args"] == ["-config", "/etc/spiffe-helper/helper.conf"]
    assert helper["securityContext"]["runAsUser"] == 65532
    assert helper["securityContext"]["runAsNonRoot"] is True
    assert helper["securityContext"]["readOnlyRootFilesystem"] is True
    assert helper["securityContext"]["allowPrivilegeEscalation"] is False
    assert helper["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert (
        backend["securityContext"]["runAsUser"]
        == (helper["securityContext"]["runAsUser"])
    )
    assert helper["readinessProbe"]["httpGet"] == {
        "path": "/ready",
        "port": "spiffe-health",
    }
    assert pod["securityContext"]["fsGroup"] == 65532

    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["spiffe-workload-api"]["csi"] == {
        "driver": "csi.spiffe.io",
        "readOnly": True,
    }
    assert volumes["tokenbroker-svid"]["emptyDir"] == {
        "medium": "Memory",
        "sizeLimit": "1Mi",
    }
    backend_mount = next(
        mount
        for mount in backend["volumeMounts"]
        if mount["name"] == "tokenbroker-svid"
    )
    assert backend_mount["readOnly"] is True

    config = _object(objects, "ConfigMap", "monolith-tokenbroker-spiffe-helper")[
        "data"
    ]["helper.conf"]
    assert 'hint = "monolith-platform"' in config
    cert_mode = _file_mode(config, "cert_file_mode")
    key_mode = _file_mode(config, "key_file_mode")
    assert cert_mode & 0o200, "certificate must remain owner-writable for rotation"
    assert key_mode & 0o200, "key must remain owner-writable for rotation"
    assert cert_mode & 0o400, "same-UID backend must be able to read certificate"
    assert key_mode & 0o400, "same-UID backend must be able to read key"
    assert key_mode & 0o007 == 0, "key must grant no permissions to other users"
    assert 'agent_address = "/spiffe-workload-api/spire-agent.sock"' in config


def test_enabled_render_rejects_non_tls_url_and_missing_server_identity() -> None:
    for settings, message in [
        (
            [
                "tokenBroker.spiffe.enabled=true",
                "tokenBroker.spiffe.url=http://broker:8080",
            ],
            "must use https",
        ),
        (
            [
                "tokenBroker.spiffe.enabled=true",
                "tokenBroker.spiffe.serverId=",
            ],
            "serverId is required",
        ),
    ]:
        try:
            _render("invalid", settings)
        except RuntimeError as error:
            assert message in str(error)
        else:
            raise AssertionError(f"accepted invalid SPIFFE settings: {settings}")


def test_all_checked_in_environment_overlays_keep_spiffe_default_off() -> None:
    projects = _chart_dir().parents[1]
    paths = [
        _chart_dir() / "values.yaml",
        projects / "monolith/deploy/values.yaml",
        projects / "monolith/deploy/values-gke.yaml",
        projects / "monolith/dev/deploy/values.yaml",
        projects / "monolith/dev/deploy/values-recovery-gke.yaml",
    ]
    for path in paths:
        values = yaml.safe_load(path.read_text())
        assert values["tokenBroker"]["spiffe"]["enabled"] is False, path


def _quota_job_settings() -> list[str]:
    return [
        "jobs.image.repository=registry.example/monolith-jobs",
        "jobs.image.digest=sha256:" + "a" * 64,
    ]


def _job_container(workflow: dict) -> dict:
    return workflow["spec"]["workflowSpec"]["templates"][0]["container"]


def test_only_factory_snapshot_receives_configured_provider_quota_transport() -> None:
    objects = _render(
        "monolith",
        _quota_job_settings() + ["tokenBroker.url=http://configured-broker:8080"],
    )
    workflows = [obj for obj in objects if obj.get("kind") == "CronWorkflow"]
    quota_workflows = [
        workflow
        for workflow in workflows
        if "EMBER_TOKENBROKER_URL" in _env(_job_container(workflow))
    ]

    assert [workflow["metadata"]["name"] for workflow in quota_workflows] == [
        "factory-public-snapshot"
    ]
    workflow = quota_workflows[0]
    env = _env(_job_container(workflow))
    assert env["EMBER_TOKENBROKER_URL"] == "http://configured-broker:8080"
    assert env["EMBER_TOKENBROKER_SPIFFE_ENABLED"] == "false"
    assert not any(name.startswith("EMBER_TOKENBROKER_SVID_") for name in env)
    assert workflow["spec"]["workflowSpec"]["serviceAccountName"] == "argo-workflow"
    assert workflow["spec"]["schedules"] == ["*/2 * * * *"]


def test_unconfigured_broker_omits_provider_quota_transport() -> None:
    objects = _render("monolith", _quota_job_settings() + ["tokenBroker.url="])
    workflow = _object(objects, "CronWorkflow", "factory-public-snapshot")

    assert not any(
        name.startswith("EMBER_TOKENBROKER_") for name in _env(_job_container(workflow))
    )


def test_quota_job_preserves_spiffe_transport_without_granting_job_identity() -> None:
    objects = _render(
        "monolith",
        _quota_job_settings()
        + [
            "tokenBroker.spiffe.enabled=true",
            "tokenBroker.url=http://plaintext-broker:8080",
            "tokenBroker.spiffe.url=https://secure-broker:8443",
            "tokenBroker.spiffe.serverId=spiffe://example.test/broker",
        ],
    )
    workflow = _object(objects, "CronWorkflow", "factory-public-snapshot")
    spec = workflow["spec"]["workflowSpec"]
    container = _job_container(workflow)
    env = _env(container)

    assert env["EMBER_TOKENBROKER_URL"] == "https://secure-broker:8443"
    assert env["EMBER_TOKENBROKER_SPIFFE_ENABLED"] == "true"
    assert env["EMBER_TOKENBROKER_SPIFFE_ID"] == "spiffe://example.test/broker"
    assert env["EMBER_TOKENBROKER_SVID_CERT"] == "/run/tokenbroker-svid/svid.pem"
    assert env["EMBER_TOKENBROKER_SVID_KEY"] == "/run/tokenbroker-svid/svid_key.pem"
    assert env["EMBER_TOKENBROKER_SVID_BUNDLE"] == (
        "/run/tokenbroker-svid/svid_bundle.pem"
    )
    assert "http://plaintext-broker:8080" not in env.values()
    assert "volumes" not in spec
    assert "volumeMounts" not in container
    assert "sidecars" not in spec["templates"][0]
    assert spec["serviceAccountName"] == "argo-workflow"
    assert [
        entry["name"]
        for entry in container["env"]
        if "secretKeyRef" in entry.get("valueFrom", {})
    ] == ["DATABASE_URL"]

    for other in objects:
        if other.get("kind") != "CronWorkflow" or other is workflow:
            continue
        assert not any(
            name.startswith("EMBER_TOKENBROKER_")
            for name in _env(_job_container(other))
        ), other["metadata"]["name"]


def test_codex_account_admission_activation_is_explicit_and_reachable() -> None:
    for settings, enabled, preferred in [
        ([], "false", ""),
        (
            [
                "swarm.codexExhaustedGrantExclusion=true",
                "swarm.codexPreferredGrant=codex-b",
            ],
            "true",
            "codex-b",
        ),
    ]:
        objects = _render("monolith", ["swarm.enabled=true", *settings])
        env = _env(_container(_object(objects, "Deployment", "monolith"), "backend"))
        assert env["SWARM_CODEX_EXHAUSTED_GRANT_EXCLUSION"] == enabled
        assert env["SWARM_CODEX_PREFERRED_GRANT"] == preferred
