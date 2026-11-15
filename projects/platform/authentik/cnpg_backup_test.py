"""Factory HA/backups and byte-identical hub rendering (HOME-37, #7952)."""

import difflib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent


def render(overlay, chart=CHART, *settings):
    command = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "authentik",
        str(chart),
        "--namespace",
        "authentik",
        "-f",
        str(chart / "values.yaml"),
        "-f",
        str(chart / f"values-{overlay}.yaml"),
    ]
    for setting in settings:
        command.extend(["--set", setting])
    return subprocess.run(command, capture_output=True, check=True, timeout=120).stdout


def documents(output):
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(output)
        if doc
    }


def test_factory_ha_and_garage_backups():
    docs = documents(render("factory"))
    cluster = docs["Cluster", "authentik-pg"]
    assert cluster["metadata"]["namespace"] == "authentik"
    assert cluster["metadata"]["annotations"] == {
        "argocd.argoproj.io/sync-wave": "0",
        "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
    }
    spec = cluster["spec"]
    assert spec["instances"] == 3
    assert spec["affinity"] == {
        "enablePodAntiAffinity": True,
        "podAntiAffinityType": "required",
        "topologyKey": "kubernetes.io/hostname",
    }
    # Expand the existing database in place, preserving its bootstrap and PVC size.
    assert spec["bootstrap"] == {
        "initdb": {"database": "authentik", "owner": "authentik"}
    }
    assert spec["storage"] == {"size": "5Gi"}
    assert spec["backup"] == {
        "retentionPolicy": "14d",
        "barmanObjectStore": {
            "destinationPath": "s3://authentik-pg-backups/",
            "serverName": "authentik-pg-v1",
            # A custom endpoint selects Boto3's path-style S3 addressing.
            "endpointURL": "http://garage.garage.svc:3900",
            "s3Credentials": {
                "accessKeyId": {"name": "authentik-pg-s3", "key": "AWS_ACCESS_KEY_ID"},
                "secretAccessKey": {
                    "name": "authentik-pg-s3",
                    "key": "AWS_SECRET_ACCESS_KEY",
                },
                "region": {"name": "authentik-pg-s3", "key": "AWS_REGION"},
            },
            "wal": {"compression": "gzip"},
            "data": {"compression": "gzip"},
        },
    }
    scheduled = docs["ScheduledBackup", "authentik-pg-daily"]
    assert scheduled["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "1"
    assert scheduled["spec"] == {
        "schedule": "0 0 3 * * *",
        "immediate": True,
        "backupOwnerReference": "self",
        "method": "barmanObjectStore",
        "cluster": {"name": "authentik-pg"},
    }
    assert not any(kind == "OnePasswordItem" for kind, _ in docs)


def test_hub_render_unchanged(tmp_path):
    # These are the three pre-S3 templates from 4176392aa. Render the complete
    # chart with those templates and the same hub values, then compare bytes.
    before = tmp_path / "before"
    shutil.copytree(CHART, before)
    for template in (CHART / "testdata/cnpg-before").glob("*.yaml"):
        shutil.copyfile(template, before / "templates" / template.name)
    old = render("gke", before)
    new = render("gke")
    assert old == new, "".join(
        difflib.unified_diff(
            old.decode().splitlines(keepends=True),
            new.decode().splitlines(keepends=True),
            fromfile="hub-before",
            tofile="hub-after",
        )
    )
    spec = documents(new)["Cluster", "authentik-pg"]["spec"]
    assert spec["instances"] == 1
    assert "affinity" not in spec
    store = spec["backup"]["barmanObjectStore"]
    assert store["serverName"] == "authentik-pg-gke"
    assert store["googleCredentials"] == {
        "applicationCredentials": {
            "name": "authentik-pg-backup-gcs",
            "key": "service-account-key.json",
        }
    }
    assert "s3Credentials" not in store


@pytest.mark.parametrize("overlay", ["factory", "gke"])
def test_backups_can_be_disabled(overlay):
    docs = documents(render(overlay, CHART, "cnpg.backup.enabled=false"))
    assert "backup" not in docs["Cluster", "authentik-pg"]["spec"]
    assert ("ScheduledBackup", "authentik-pg-daily") not in docs
    assert ("OnePasswordItem", "authentik-pg-backup-gcs") not in docs


def test_s3_requires_an_endpoint():
    with pytest.raises(subprocess.CalledProcessError) as error:
        render("factory", CHART, "cnpg.backup.endpointURL=")
    assert b"cnpg.backup.endpointURL is required for S3" in error.value.stderr
