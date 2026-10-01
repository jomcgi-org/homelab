"""Guard the default-off loom raw manifest set without cluster access."""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parent
MANIFESTS = [
    "namespace.yaml",
    "cnpg-cluster.yaml",
    "cnpg-scheduledbackup.yaml",
    "onepassworditem-cnpg-backup-gcs.yaml",
    "cnpg-metrics-service.yaml",
    "httproute.yaml",
    "image-pull-secret.yaml",
    "onepassworditem-r2.yaml",
    "argocd-repo-cred.yaml",
]


@pytest.fixture
def documents():
    return {
        path.name: list(yaml.safe_load_all(path.read_text()))
        for path in DEPLOY.glob("*.yaml")
        if path.name != "values.yaml"
    }


@pytest.fixture
def cluster(documents):
    return documents["cnpg-cluster.yaml"][0]


def test_kustomization_has_exact_complete_resource_set(documents):
    for name, parsed in documents.items():
        assert len(parsed) == (2 if name == "httproute.yaml" else 1)
        assert all(isinstance(document, dict) for document in parsed)
    kustomization = documents["kustomization.yaml"][0]
    assert kustomization["apiVersion"] == "kustomize.config.k8s.io/v1beta1"
    assert kustomization["kind"] == "Kustomization"
    assert kustomization["resources"] == MANIFESTS
    assert all((DEPLOY / name).is_file() for name in MANIFESTS)
    assert set(documents) - {"kustomization.yaml"} == set(MANIFESTS)
    assert {path.name for path in DEPLOY.glob("*.yaml")} == set(MANIFESTS) | {
        "kustomization.yaml",
        "values.yaml",
    }


def test_resources_belong_to_loom_namespace(documents):
    namespace = documents["namespace.yaml"][0]
    assert namespace["apiVersion"] == "v1"
    assert namespace["kind"] == "Namespace"
    assert namespace["metadata"]["name"] == "loom"
    for name in MANIFESTS:
        for document in documents[name]:
            if document["kind"] != "Namespace":
                expected = "argocd" if name == "argocd-repo-cred.yaml" else "loom"
                assert document["metadata"]["namespace"] == expected


def test_cluster_identity_and_pg17_system_image(cluster):
    assert cluster["kind"] == "Cluster"
    assert cluster["apiVersion"] == "postgresql.cnpg.io/v1"
    assert cluster["metadata"]["name"] == "loom-pg"
    assert re.match(
        r"^ghcr\.io/cloudnative-pg/postgresql:17\.\d+-system-",
        cluster["spec"]["imageName"],
    )


def test_cluster_bootstrap_storage_and_resources(cluster):
    spec = cluster["spec"]
    assert spec["instances"] == 1
    assert spec["storage"] == {"size": "10Gi", "storageClass": "standard-rwo"}
    assert spec["bootstrap"] == {"initdb": {"database": "loom", "owner": "loom"}}
    assert spec["resources"]["requests"] == {"cpu": "50m", "memory": "256Mi"}
    assert spec["resources"]["limits"]["memory"] == "512Mi"
    assert "cpu" not in spec["resources"]["limits"]
    assert spec["monitoring"]["enablePodMonitor"] is True


def test_backup_uses_loom_archive_with_retention_and_compression(cluster):
    backup = cluster["spec"]["backup"]
    store = backup["barmanObjectStore"]
    assert store["destinationPath"] == "gs://h0melab-cnpg-backups/loom-pg/"
    assert store["serverName"] == "loom-pg"
    assert store["wal"]["compression"] == "gzip"
    assert store["data"]["compression"] == "gzip"
    assert backup["retentionPolicy"] == "14d"


def test_scheduled_backup_targets_cluster_immediately(documents, cluster):
    scheduled = documents["cnpg-scheduledbackup.yaml"][0]
    assert scheduled["kind"] == "ScheduledBackup"
    assert scheduled["apiVersion"] == "postgresql.cnpg.io/v1"
    assert scheduled["metadata"]["name"] == "loom-pg-daily"
    assert scheduled["spec"]["cluster"]["name"] == cluster["metadata"]["name"]
    assert scheduled["spec"]["schedule"] == "0 0 2 * * *"
    assert scheduled["spec"]["immediate"] is True
    assert scheduled["spec"]["backupOwnerReference"] == "self"


def test_backup_credential_references_existing_onepassword_item(documents, cluster):
    item = documents["onepassworditem-cnpg-backup-gcs.yaml"][0]
    assert item["kind"] == "OnePasswordItem"
    assert item["apiVersion"] == "onepassword.com/v1"
    assert item["metadata"]["name"] == "loom-pg-backup-gcs"
    credentials = cluster["spec"]["backup"]["barmanObjectStore"]["googleCredentials"]
    assert credentials["applicationCredentials"] == {
        "name": item["metadata"]["name"],
        "key": "service-account-key.json",
    }
    assert item["spec"]["itemPath"].startswith("vaults/k8s-homelab/items/")
    assert item["spec"]["itemPath"] == (
        "vaults/k8s-homelab/items/cnpg-gcs-backups-context-forge"
    )


def assert_no_plaintext_credential(value):
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in {"password", "token", "privateKey", "private_key"}
            assert_no_plaintext_credential(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_plaintext_credential(child)
    elif isinstance(value, str):
        assert "BEGIN PRIVATE KEY" not in value
        assert '"private_key"' not in value


def test_no_plaintext_credentials_anywhere_in_manifests(documents):
    for name, parsed in documents.items():
        raw = (DEPLOY / name).read_text()
        assert "BEGIN PRIVATE KEY" not in raw
        assert '"private_key"' not in raw
        for document in parsed:
            assert document["kind"] != "Secret"
            assert "data" not in document
            assert "stringData" not in document
            assert_no_plaintext_credential(document)


def test_metrics_service_selects_cluster_and_exposes_metrics(documents, cluster):
    service = documents["cnpg-metrics-service.yaml"][0]
    assert service["kind"] == "Service"
    assert service["apiVersion"] == "v1"
    assert service["metadata"]["name"] == "loom-pg-metrics"
    assert service["metadata"]["labels"]["app.kubernetes.io/component"] == (
        "database-metrics"
    )
    assert service["spec"]["selector"] == {
        "cnpg.io/cluster": cluster["metadata"]["name"]
    }
    assert service["spec"]["ports"] == [
        {"name": "metrics", "port": 9187, "targetPort": 9187, "protocol": "TCP"}
    ]


def test_default_off_has_no_application(documents):
    for parsed in documents.values():
        for document in parsed:
            assert document["kind"] != "Application"


def test_default_off_home_generator_never_enrolls_loom(tmp_path):
    generator = Path(
        os.environ.get(
            "HOME_CLUSTER_GENERATOR_BIN",
            str(DEPLOY.parents[2] / "bazel/images/generate-home-cluster.sh"),
        )
    ).resolve()
    for project in ("loom", "inference"):
        deploy = tmp_path / "projects" / project / "deploy"
        deploy.mkdir(parents=True)
        (deploy / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\n"
            "kind: Kustomization\nresources: []\n"
        )
    subprocess.run(
        ["bash", str(generator)],
        cwd=tmp_path,
        env={**os.environ, "BUILD_WORKSPACE_DIRECTORY": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
    )
    generated = yaml.safe_load(
        (tmp_path / "projects/home-cluster/kustomization.yaml").read_text()
    )
    assert generated["resources"] == ["../../projects/inference/deploy"]
