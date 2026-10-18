"""Guard the default-off loom raw manifest set without cluster access."""

import copy
import re
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
        if name == "httproute.yaml":
            assert_trusted_ingress(parsed)
        else:
            assert len(parsed) == 1
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


def assert_trusted_ingress(documents):
    resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in documents}
    assert len(documents) == len(resources) == 3
    assert set(resources) == {
        ("HTTPRoute", "loom-private"),
        ("HTTPRoute", "loom-private-slash-redirect"),
        ("SecurityPolicy", "loom-private-cf-access"),
    }
    for (kind, _), resource in resources.items():
        assert resource["metadata"]["namespace"] == "loom"
        if kind == "HTTPRoute":
            assert resource["apiVersion"] == "gateway.networking.k8s.io/v1"
            assert resource["metadata"].get("labels", {}).get("ingress-tier") == (
                "trusted"
            )
        else:
            assert resource["apiVersion"] == "gateway.envoyproxy.io/v1alpha1"
    policy = resources[("SecurityPolicy", "loom-private-cf-access")]
    # Exact equality also rejects optional JWT validation or extra targets.
    assert policy["spec"] == {
        "targetRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "name": "loom-private",
            }
        ],
        "jwt": {
            "providers": [
                {
                    "name": "cloudflare-access",
                    "issuer": "https://jomcgi.cloudflareaccess.com",
                    "remoteJWKS": {
                        "cacheDuration": "300s",
                        "uri": "https://jomcgi.cloudflareaccess.com/cdn-cgi/access/certs",
                    },
                    "extractFrom": {"headers": [{"name": "Cf-Access-Jwt-Assertion"}]},
                    "claimToHeaders": [{"claim": "email", "header": "X-Auth-Email"}],
                }
            ]
        },
    }
    # The untargeted route must never become a second backend path.
    redirect = resources[("HTTPRoute", "loom-private-slash-redirect")]
    assert len(redirect["spec"]["rules"]) == 1
    rule = redirect["spec"]["rules"][0]
    assert set(rule) == {"matches", "filters"}
    assert [entry["type"] for entry in rule["filters"]] == ["RequestRedirect"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_policy",
        "duplicate_route",
        "missing_route_label",
        "missing_redirect_label",
        "wrong_namespace",
        "wrong_target",
        "wrong_target_kind",
        "optional_jwt",
        "wrong_issuer",
        "wrong_jwks",
        "missing_cache_default",
        "wrong_header",
        "missing_email_projection",
        "redirect_backend",
    ],
)
def test_ingress_mutations_fail_acceptance(documents, mutation):
    ingress = copy.deepcopy(documents["httproute.yaml"])
    resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in ingress}
    route = resources[("HTTPRoute", "loom-private")]
    redirect = resources[("HTTPRoute", "loom-private-slash-redirect")]
    policy = resources[("SecurityPolicy", "loom-private-cf-access")]
    provider = policy["spec"]["jwt"]["providers"][0]
    if mutation == "missing_policy":
        ingress.remove(policy)
    elif mutation == "duplicate_route":
        ingress.append(copy.deepcopy(route))
    elif mutation == "missing_route_label":
        del route["metadata"]["labels"]
    elif mutation == "missing_redirect_label":
        del redirect["metadata"]["labels"]
    elif mutation == "wrong_namespace":
        policy["metadata"]["namespace"] = "other"
    elif mutation == "wrong_target":
        policy["spec"]["targetRefs"][0]["name"] = "loom-private-slash-redirect"
    elif mutation == "wrong_target_kind":
        policy["spec"]["targetRefs"][0]["kind"] = "Gateway"
    elif mutation == "optional_jwt":
        policy["spec"]["jwt"]["optional"] = True
    elif mutation == "wrong_issuer":
        provider["issuer"] = "https://other.cloudflareaccess.com"
    elif mutation == "wrong_jwks":
        provider["remoteJWKS"]["uri"] = "https://other.cloudflareaccess.com/certs"
    elif mutation == "missing_cache_default":
        del provider["remoteJWKS"]["cacheDuration"]
    elif mutation == "wrong_header":
        provider["extractFrom"]["headers"][0]["name"] = "Authorization"
    elif mutation == "missing_email_projection":
        del provider["claimToHeaders"]
    elif mutation == "redirect_backend":
        redirect["spec"]["rules"][0]["backendRefs"] = route["spec"]["rules"][0][
            "backendRefs"
        ]
    with pytest.raises(AssertionError):
        assert_trusted_ingress(ingress)


def test_resources_belong_to_loom_namespace(documents):
    namespace = documents["namespace.yaml"][0]
    assert namespace["apiVersion"] == "v1"
    assert namespace["kind"] == "Namespace"
    assert namespace["metadata"]["name"] == "loom"
    for name in MANIFESTS:
        for document in documents[name]:
            if document["kind"] != "Namespace":
                assert document["metadata"]["namespace"] == "loom"


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
