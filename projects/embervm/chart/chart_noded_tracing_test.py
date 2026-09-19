"""Render-test noded tracing across every shared pod-template consumer."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

CHART_DIR = Path(__file__).resolve().parent
DEFAULT_VALUES = CHART_DIR / "values.yaml"


def _external_values(env_name: str, fallback: Path) -> Path:
    configured = os.environ.get(env_name)
    path = Path(configured) if configured else fallback
    if not path.exists():
        raise RuntimeError(f"{env_name} values file does not exist: {path}")
    return path


def _prod_values() -> Path:
    return _external_values(
        "DEPLOY_VALUES", CHART_DIR.parent / "deploy" / "values.yaml"
    )


def _collector_prod_values() -> Path:
    return _external_values(
        "COLLECTOR_PROD_VALUES",
        CHART_DIR.parents[1] / "platform" / "otel-collector" / "values-prod.yaml",
    )


def _render(values: list[Path], sets: list[str] | None = None) -> list[dict]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "noded-tracing",
        str(CHART_DIR),
        "--namespace",
        "embervm",
    ]
    for values_file in values:
        argv.extend(("--values", str(values_file)))
    for setting in sets or []:
        argv.extend(("--set", setting))
    result = subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _noded_containers(objects: list[dict]) -> list[tuple[str, str, dict]]:
    containers = []
    for obj in objects:
        if obj.get("kind") not in {"DaemonSet", "Deployment"}:
            continue
        pod_containers = (
            obj.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        for container in pod_containers:
            if container.get("name") == "noded":
                containers.append((obj["kind"], obj["metadata"]["name"], container))
    return containers


def _env(container: dict) -> dict[str, str | None]:
    return {entry["name"]: entry.get("value") for entry in container["env"]}


def test_default_endpoint_keeps_noded_tracing_env_absent() -> None:
    containers = _noded_containers(_render([DEFAULT_VALUES]))
    assert containers, "default chart rendered no noded pod"
    for _, name, container in containers:
        env = _env(container)
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env, name
        assert "OTEL_SERVICE_NAME" not in env, name
        assert "OTEL_SERVICE_VERSION" not in env, name


def test_configured_endpoint_uses_default_noded_service_name() -> None:
    objects = _render(
        [DEFAULT_VALUES],
        ["noded.tracing.endpoint=http://collector.example.test:4317"],
    )
    containers = _noded_containers(objects)
    assert containers
    image_digest = yaml.safe_load(DEFAULT_VALUES.read_text())["noded"]["image"][
        "digest"
    ]
    for _, name, container in containers:
        env = _env(container)
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
            "http://collector.example.test:4317"
        ), name
        assert env["OTEL_SERVICE_NAME"] == "embervm-noded", name
        assert env["OTEL_SERVICE_VERSION"] == image_digest, name


def test_production_tracing_reaches_every_shared_template_consumer() -> None:
    objects = _render([DEFAULT_VALUES, _prod_values()], ["noded.enabled=true"])
    containers = _noded_containers(objects)
    identities = {(kind, name) for kind, name, _ in containers}
    image_digest = yaml.safe_load(DEFAULT_VALUES.read_text())["noded"]["image"][
        "digest"
    ]

    assert any(kind == "DaemonSet" for kind, _ in identities)
    assert any(
        kind == "Deployment" and name.endswith("-brick-2gi")
        for kind, name in identities
    )
    assert any(
        kind == "Deployment" and name.endswith("-brick-2gi-node-1")
        for kind, name in identities
    )

    for _, name, container in containers:
        env = _env(container)
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
            "http://otel-collector.otel-collector.svc.cluster.local:4317"
        ), name
        assert env["OTEL_SERVICE_NAME"] == "embervm-noded", name
        assert env["OTEL_SERVICE_VERSION"] == image_digest, name
        assert env["OTEL_SERVICE_VERSION"] != "dev", name


def test_production_service_name_is_collector_allowlisted() -> None:
    deploy_values = yaml.safe_load(_prod_values().read_text())
    collector_values = yaml.safe_load(_collector_prod_values().read_text())
    tracing = deploy_values["noded"]["tracing"]

    assert tracing["endpoint"]
    assert tracing["endpoint"] == deploy_values["tracing"]["endpoint"]
    assert tracing["serviceName"] in collector_values["allowedServices"]
