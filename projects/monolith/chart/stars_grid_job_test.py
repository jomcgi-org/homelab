"""Rendered contract for the dedicated stars grid CronWorkflow."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def test_stars_grid_job_is_isolated_suspended_and_configurable(tmp_path):
    chart = Path(__file__).resolve().parent
    override = tmp_path / "grid-values.yaml"
    override.write_text(
        "stars:\n"
        "  gridGenerator:\n"
        "    enabled: true\n"
        "    image:\n"
        "      repository: registry.invalid/stars-grid\n"
        "      digest: sha256:test\n"
        "    source:\n"
        "      endpoint: https://objects.invalid\n"
        "      region: test-region\n"
        "      bucket: source\n"
        "      admin1Key: boundary.geojson\n"
        "      roadsKey: roads.geojson\n"
        "      lightPollutionKey: light.tif\n"
        "      demKey: dem.tif\n"
    )
    helm = os.environ.get("HELM_BIN", "helm")
    result = subprocess.run(
        [
            helm,
            "template",
            "monolith",
            str(chart),
            "--namespace",
            "monolith",
            "--values",
            str(chart / "values.yaml"),
            "--values",
            str(override),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    workflow = next(
        doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc and doc.get("metadata", {}).get("name") == "stars-grid-compute"
    )

    assert workflow["kind"] == "CronWorkflow"
    assert workflow["spec"]["suspend"] is True
    assert workflow["spec"]["concurrencyPolicy"] == "Forbid"
    spec = workflow["spec"]["workflowSpec"]
    assert spec["securityContext"]["runAsUser"] == 65532
    template = spec["templates"][0]
    container = template["container"]
    assert container["image"] == "registry.invalid/stars-grid@sha256:test"
    assert "args" not in container
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["STARS_GRID_SOURCE_S3_BUCKET"]["value"] == "source"
    assert env["STARS_GRID_LIGHT_POLLUTION_KEY"]["value"] == "light.tif"
    assert env["STARS_GRID_ROADS_KEY"]["value"] == "roads.geojson"
    assert env["STARS_GRID_DEM_KEY"]["value"] == "dem.tif"
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"]
    assert spec["volumes"][0]["emptyDir"]["sizeLimit"] == "5Gi"


def test_production_keeps_grid_job_suspended_and_dev_omits_it():
    production = yaml.safe_load(Path(os.environ["DEPLOY_VALUES"]).read_text())
    development = yaml.safe_load(Path(os.environ["DEV_VALUES"]).read_text())

    prod_job = production["stars"]["gridGenerator"]
    assert prod_job == {"enabled": True, "suspend": True}
    assert development["stars"]["gridGenerator"]["enabled"] is False
