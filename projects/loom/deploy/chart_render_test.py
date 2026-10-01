"""Literal acceptance and mutation guards for the source-pinned Loom chart."""

import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parent
APPLICATION = Path(
    os.environ.get(
        "LOOM_APPLICATION",
        str(DEPLOY.parents[2] / "projects/gke-apps/loom/application.yaml"),
    )
)


def render(values_path):
    chart = Path(os.environ["LOOM_CHART_YAML"]).parent
    output = subprocess.run(
        [
            os.environ["HELM_BIN"],
            "template",
            "loom",
            str(chart),
            "-n",
            "loom",
            "-f",
            str(values_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [document for document in yaml.safe_load_all(output) if document]


@pytest.fixture(scope="module")
def rendered():
    return render(DEPLOY / "values.yaml")


def test_default_probe_matches_the_query_api_service(rendered):
    values_path = Path(
        os.environ.get(
            "LOOM_MONITORING_VALUES",
            str(DEPLOY.parents[2] / "projects/platform/otel-collector/values.yaml"),
        )
    )
    monitoring = yaml.safe_load(values_path.read_text())["loom"]
    assert monitoring["enabled"] is False
    endpoint = urlsplit(monitoring["probeEndpoint"])
    assert endpoint.scheme == "http"
    assert endpoint.path == "/docs"
    assert endpoint.hostname == f"loom-query-api.{monitoring['namespace']}.svc"
    service = next(
        d
        for d in rendered
        if d["kind"] == "Service" and d["metadata"]["name"] == "loom-query-api"
    )
    # Helm assigns --namespace loom to resources without an explicit namespace.
    assert service["metadata"].get("namespace", "loom") == monitoring["namespace"]
    assert endpoint.port == 8080
    assert any(p["port"] == endpoint.port for p in service["spec"]["ports"])
    assert not any(d["kind"] == "NetworkPolicy" for d in rendered)


def containers(documents):
    result = {}
    for document in documents:
        kind = document["kind"]
        spec = document.get("spec", {})
        if kind == "Pod":
            pod = spec
        elif kind == "CronJob":
            pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        elif "template" in spec:
            pod = spec["template"]["spec"]
        else:
            continue
        for field in ("containers", "initContainers", "ephemeralContainers"):
            for container in pod.get(field, []):
                identity = (
                    kind,
                    document["metadata"]["name"],
                    field,
                    container["name"],
                )
                assert identity not in result
                result[identity] = container
    return result


def assert_containers_and_resources(documents):
    actual = containers(documents)
    expected = {
        ("Deployment", "loom-ingest", "containers", "ingest"): ("100m", "512Mi"),
        ("Deployment", "loom-query-api", "containers", "query-api"): ("100m", "512Mi"),
        ("Deployment", "loom-query-api", "containers", "engine"): ("200m", "1Gi"),
        ("Deployment", "loom-worker", "containers", "worker"): ("100m", "512Mi"),
        ("Deployment", "loom-worker", "containers", "engine"): ("200m", "1Gi"),
    }
    assert set(actual) == set(expected)
    for identity, (cpu, memory) in expected.items():
        assert actual[identity]["resources"] == {
            "requests": {"cpu": cpu, "memory": memory},
            "limits": {"memory": memory},
        }


def assert_external_postgres_and_migrations(documents):
    assert not {"NetworkPolicy", "Cluster", "Job", "PersistentVolumeClaim"} & {
        document["kind"] for document in documents
    }
    keys = {
        "LOOM_DB_HOST": "host",
        "LOOM_DB_PORT": "port",
        "LOOM_DB_USER": "username",
        "LOOM_DB_PASSWORD": "password",
        "LOOM_DB_NAME": "dbname",
    }
    for identity, container in containers(documents).items():
        env = {entry["name"]: entry for entry in container["env"]}
        database_env = {name for name in env if name.startswith("LOOM_DB_")}
        if identity[-1] == "worker":
            # Upstream's zero-pool worker delegates Postgres and migrations to
            # its engine sidecar. Giving it DB credentials is a regression.
            assert database_env == set()
            continue
        assert database_env == set(keys) | {"LOOM_DB_MIGRATE_ON_BOOT"}
        assert env["LOOM_DB_MIGRATE_ON_BOOT"] == {
            "name": "LOOM_DB_MIGRATE_ON_BOOT",
            "value": "true",
        }
        for name, key in keys.items():
            assert env[name] == {
                "name": name,
                "valueFrom": {"secretKeyRef": {"name": "loom-pg-app", "key": key}},
            }


def assert_r2_and_images(documents):
    for identity, container in containers(documents).items():
        env = {entry["name"]: entry for entry in container["env"]}
        for name, value in {
            "AWS_ENDPOINT_URL": "https://7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com",
            "AWS_REGION": "auto",
            "LOOM_WAREHOUSE_URI": "s3://loom",
        }.items():
            assert env[name] == {"name": name, "value": value}
        for name, key in {
            "AWS_ACCESS_KEY_ID": "access-key-id",
            "AWS_SECRET_ACCESS_KEY": "secret-access-key",
        }.items():
            assert env[name] == {
                "name": name,
                "valueFrom": {
                    "secretKeyRef": {"name": "loom-s3-credentials", "key": key}
                },
            }
        assert (
            container["image"] == f"ghcr.io/weave-hand/loom-{identity[-1]}:sha-e6ca13c"
        )
        assert "@sha256" not in container["image"]
        assert container["imagePullPolicy"] == "IfNotPresent"


def test_chart_and_application_pin_are_literal_0_2_0():
    chart = yaml.safe_load(Path(os.environ["LOOM_CHART_YAML"]).read_text())
    app = yaml.safe_load(APPLICATION.read_text())
    assert chart["version"] == app["spec"]["sources"][0]["targetRevision"] == "0.2.0"
    assert app["spec"]["sources"][0]["targetRevision"] not in {
        "0.0.0-edge",
        "bleeding-edge",
    }


def test_exact_container_set_and_literal_resources(rendered):
    assert_containers_and_resources(rendered)


def test_external_postgres_on_boot_without_chart_policies_or_storage(rendered):
    assert_external_postgres_and_migrations(rendered)


def test_r2_and_all_four_image_pins(rendered):
    assert_r2_and_images(rendered)
    item = yaml.safe_load((DEPLOY / "onepassworditem-r2.yaml").read_text())
    assert item["kind"] == "OnePasswordItem"
    assert item["metadata"]["name"] == "loom-s3-credentials"
    assert item["metadata"]["namespace"] == "loom"
    assert item["spec"]["itemPath"] == "vaults/k8s-homelab/items/r2-s3-credentials"


def test_created_service_account_and_pull_item(rendered):
    accounts = [doc for doc in rendered if doc["kind"] == "ServiceAccount"]
    assert len(accounts) == 1
    assert accounts[0]["metadata"]["name"] == "loom"
    assert accounts[0]["imagePullSecrets"] == [{"name": "ghcr-imagepull-secret"}]
    for doc in rendered:
        if doc["kind"] == "Deployment":
            assert doc["spec"]["template"]["spec"]["serviceAccountName"] == "loom"
    item = yaml.safe_load((DEPLOY / "image-pull-secret.yaml").read_text())
    assert item["kind"] == "OnePasswordItem"
    assert item["type"] == "kubernetes.io/dockerconfigjson"
    assert item["metadata"]["name"] == "ghcr-imagepull-secret"
    assert item["metadata"]["namespace"] == "loom"
    assert item["spec"]["itemPath"] == "vaults/k8s-homelab/items/ghcr-read-permissions"


def test_worker_fallback_is_zero(rendered):
    workers = [doc for doc in rendered if doc["metadata"]["name"] == "loom-worker"]
    assert len(workers) == 1
    assert workers[0]["spec"]["replicas"] == 0


def test_ui_prefix_and_config_mount(rendered):
    config = next(doc for doc in rendered if doc["kind"] == "ConfigMap")
    assert config["metadata"]["name"] == "loom-ui-config"
    assert (
        config["data"]["config.js"]
        == 'window.LOOM_CONFIG = { apiBase: "/app/loom" };\n'
    )
    query = next(
        doc
        for doc in rendered
        if doc["metadata"]["name"] == "loom-query-api" and doc["kind"] == "Deployment"
    )
    pod = query["spec"]["template"]["spec"]
    assert {"name": "ui-config", "configMap": {"name": "loom-ui-config"}} in pod[
        "volumes"
    ]
    api = next(
        container for container in pod["containers"] if container["name"] == "query-api"
    )
    assert {
        "name": "ui-config",
        "mountPath": "/usr/share/loom/ui/config.js",
        "subPath": "config.js",
    } in api["volumeMounts"]


def test_private_route_matches_rendered_service_and_redirect(rendered):
    routes = list(yaml.safe_load_all((DEPLOY / "httproute.yaml").read_text()))
    assert [route["metadata"]["name"] for route in routes] == [
        "loom-private",
        "loom-private-slash-redirect",
    ]
    for route in routes:
        assert route["spec"]["parentRefs"] == [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "Gateway",
                "name": "cloudflare-ingress",
                "namespace": "envoy-gateway-system",
            }
        ]
        assert route["spec"]["hostnames"] == ["private.jomcgi.dev"]
    rewrite = routes[0]["spec"]["rules"][0]
    assert rewrite["matches"] == [
        {"path": {"type": "PathPrefix", "value": "/app/loom"}}
    ]
    assert rewrite["filters"] == [
        {
            "type": "URLRewrite",
            "urlRewrite": {
                "path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/"},
            },
        }
    ]
    assert rewrite["backendRefs"] == [
        {
            "group": "",
            "kind": "Service",
            "name": "loom-query-api",
            "port": 8080,
            "weight": 1,
        }
    ]
    service = next(
        doc
        for doc in rendered
        if doc["kind"] == "Service"
        and doc["metadata"]["name"] == rewrite["backendRefs"][0]["name"]
    )
    assert service["spec"]["ports"] == [
        {"name": "http", "port": 8080, "targetPort": "http"}
    ]
    redirect = routes[1]["spec"]["rules"][0]
    assert redirect["matches"] == [{"path": {"type": "Exact", "value": "/app/loom"}}]
    assert redirect["filters"] == [
        {
            "type": "RequestRedirect",
            "requestRedirect": {
                "path": {"type": "ReplaceFullPath", "replaceFullPath": "/app/loom/"},
                "statusCode": 301,
            },
        }
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "network_policy_enabled",
        "removed_limit",
        "renamed_database_secret",
        "wrong_credentials_key",
        "digest_set",
    ],
)
def test_render_mutations_fail_acceptance(mutation, tmp_path):
    values = yaml.safe_load((DEPLOY / "values.yaml").read_text())
    if mutation == "network_policy_enabled":
        values["networkPolicy"]["enabled"] = True
    elif mutation == "removed_limit":
        del values["ingest"]["resources"]["limits"]
    elif mutation == "renamed_database_secret":
        values["postgres"]["external"]["existingSecret"] = "wrong-pg-app"
    elif mutation == "wrong_credentials_key":
        values["objectStore"]["s3"]["credentialsKeys"]["accessKeyId"] = "wrong-key"
    elif mutation == "digest_set":
        values["engine"]["image"]["digest"] = "sha256:" + "a" * 64
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values))
    documents = render(path)
    with pytest.raises(AssertionError):
        assert_containers_and_resources(documents)
        assert_external_postgres_and_migrations(documents)
        assert_r2_and_images(documents)
