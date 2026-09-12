"""Render tests for explicit EmberVM cell ownership configuration."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def _chart_dir() -> Path:
    chart = Path(__file__).resolve().parent
    if (chart / "Chart.yaml").exists():
        return chart
    raise RuntimeError("Could not find chart Chart.yaml")


def _render(*sets: str) -> list[dict]:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    command = [
        helm_bin,
        "template",
        "cell-test",
        str(_chart_dir()),
        "--include-crds",
    ]
    for value in sets:
        command.extend(("--set", value))

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _env(container: dict) -> dict[str, str]:
    return {item["name"]: item.get("value") for item in container.get("env", [])}


def test_default_cell_preserves_single_cell_addresses() -> None:
    documents = _render("noded.enabled=true")
    workloads = {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc.get("kind") in {"DaemonSet", "Deployment"}
    }

    control = workloads["cell-test-embervm"]
    control_env = _env(control["spec"]["template"]["spec"]["containers"][0])
    assert control_env["EMBERVM_CELL_ID"] == "cell-0"
    assert control_env["EMBERVM_KNOWN_CELL_IDS"] == "cell-0"

    noded = workloads["cell-test-embervm-noded"]
    noded_container = next(
        container
        for container in noded["spec"]["template"]["spec"]["containers"]
        if container["name"] == "noded"
    )
    noded_env = _env(noded_container)
    assert noded_env["EMBERVM_CELL_ID"] == "cell-0"
    assert (
        noded_env["EMBERVM_NODED_CONTROL_PLANE_URL"]
        == "http://cell-test-embervm.default.svc:8080"
    )


def test_cell_override_routes_bricks_to_its_configured_control_plane() -> None:
    documents = _render(
        "noded.enabled=true",
        "cell.id=cell-west",
        "cell.knownIds={cell-east,cell-west}",
        "cell.brickDialHomeAddress=http://cell-west-control.embervm.svc:8080",
    )
    workloads = [
        doc
        for doc in documents
        if doc.get("kind") in {"DaemonSet", "Deployment"}
    ]
    control = next(doc for doc in workloads if doc["metadata"]["name"] == "cell-test-embervm")
    noded = next(doc for doc in workloads if doc["metadata"]["name"] == "cell-test-embervm-noded")

    control_env = _env(control["spec"]["template"]["spec"]["containers"][0])
    assert control_env["EMBERVM_CELL_ID"] == "cell-west"
    assert control_env["EMBERVM_KNOWN_CELL_IDS"] == "cell-east,cell-west"

    noded_container = next(
        container
        for container in noded["spec"]["template"]["spec"]["containers"]
        if container["name"] == "noded"
    )
    noded_env = _env(noded_container)
    assert noded_env["EMBERVM_CELL_ID"] == "cell-west"
    assert (
        noded_env["EMBERVM_NODED_CONTROL_PLANE_URL"]
        == "http://cell-west-control.embervm.svc:8080"
    )


def test_workload_crd_defaults_and_fences_cell_assignment() -> None:
    crd = next(doc for doc in _render() if doc.get("kind") == "CustomResourceDefinition")
    version = crd["spec"]["versions"][0]
    spec_schema = version["schema"]["openAPIV3Schema"]["properties"]["spec"]
    cell = spec_schema["properties"]["cellId"]

    assert cell["default"] == "cell-0"
    assert cell["pattern"] == r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
    assert spec_schema["x-kubernetes-validations"] == [
        {
            "rule": "!has(oldSelf.cellId) ? self.cellId == 'cell-0' : self.cellId == oldSelf.cellId",
            "message": "spec.cellId is immutable; legacy workloads may only backfill cell-0",
        }
    ]
