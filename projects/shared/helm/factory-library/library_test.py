"""Render the public API, including refusals and offline-safe secret generation."""

import base64
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.error
import urllib.request

import pytest
import yaml

HERE = Path(__file__).resolve().parent
DIGEST = "sha256:" + "0" * 64
IMAGE = {"repository": "ghcr.io/jomcgi/example", "digest": DIGEST}
RESOURCES = {
    "requests": {"cpu": "10m", "memory": "32Mi"},
    "limits": {"memory": "128Mi"},
}


@pytest.fixture
def chart(tmp_path):
    (tmp_path / "Chart.yaml").write_text(
        "apiVersion: v2\nname: consumer\nversion: 0.1.0\n"
    )
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates/all.yaml").write_text(
        '{{ include "factory-library.all" . }}\n'
    )
    shutil.copytree(HERE / "chart", tmp_path / "charts/factory-library")
    return tmp_path


def values():
    return {
        "image": copy.deepcopy(IMAGE),
        "workloads": {"app": {"resources": copy.deepcopy(RESOURCES)}},
    }


def render(chart, config):
    path = chart / "values.yaml"
    path.write_text(yaml.safe_dump(config))
    return subprocess.run(
        [os.environ["HELM_BIN"], "template", "consumer", str(chart), "-n", "test"],
        capture_output=True,
        text=True,
        check=False,
    )


def objects(result):
    assert result.returncode == 0, result.stderr
    return {
        f"{doc['kind']}/{doc['metadata']['name']}": doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }


def golden(name, actual):
    assert actual == yaml.safe_load((HERE / "goldens" / f"{name}.yaml").read_text())


def test_defaults(chart):
    config = values()
    config["workloads"]["batch"] = {
        "kind": "Job",
        "resources": copy.deepcopy(RESOURCES),
    }
    config["workloads"]["tick"] = {
        "kind": "CronJob",
        "schedule": "0 * * * *",
        "image": {"repository": "registry.example/public", "digest": DIGEST},
        "resources": copy.deepcopy(RESOURCES),
    }
    golden("defaults", objects(render(chart, config)))


def test_declared_paths_service_api_and_peers(chart):
    config = values()
    app = config["workloads"]["app"]
    app.update(
        kubernetesApi=True,
        writablePaths=[{"name": "tmp", "mountPath": "/tmp", "sizeLimit": "16Mi"}],
        service={"ports": [{"name": "http", "port": 8080, "targetPort": "http"}]},
        ports=[{"name": "http", "containerPort": 8080}],
        network={
            "ingress": [{"peer": "edge", "port": 8080}],
            "egress": [
                {"peer": "garage", "port": 3900},
                {"peer": "itsaplan-api", "port": 3000},
                {"peer": "kagent-controller", "port": 8083},
                {"peer": "kube-apiserver", "port": 6443},
                {"peer": "github", "fqdn": "api.github.com", "port": 443},
            ],
        },
    )
    golden("declared", objects(render(chart, config)))


def test_exceptions(chart):
    config = values()
    config["exceptions"] = [
        {
            "workload": "app",
            "default": "runAsUser",
            "value": 1000,
            "reason": "Image UID",
        },
        {
            "workload": "app",
            "default": "runAsGroup",
            "value": 1000,
            "reason": "Image GID",
        },
        {
            "workload": "app",
            "default": "readOnlyRootFilesystem",
            "reason": "Legacy writer",
        },
        {"workload": "app", "default": "network.world", "reason": "External relay"},
        {"workload": "app", "default": "network.0.0.0.0/0", "reason": "IPv4 relay"},
        {"workload": "app", "default": "network.::/0", "reason": "IPv6 relay"},
        {
            "workload": "app",
            "default": "network.fqdn:*.example.com",
            "reason": "Tenant hosts",
        },
    ]
    config["workloads"]["app"]["network"] = {
        "egress": [
            {"peer": "world", "port": 443},
            {"peer": "0.0.0.0/0", "port": 443},
            {"peer": "::/0", "port": 443},
            {"peer": "tenants", "fqdn": "*.example.com", "port": 443},
        ]
    }
    docs = objects(render(chart, config))
    # The complete exception record is visible on both the workload and policy.
    for doc in docs.values():
        assert (
            json.loads(doc["metadata"]["annotations"]["factory.jomcgi.dev/exceptions"])
            == config["exceptions"]
        )
    golden("exceptions", docs)


# Error text is stable even when Helm's call stack or source line changes.
@pytest.mark.parametrize(
    "case", yaml.safe_load((HERE / "goldens/refusals.yaml").read_text())
)
def test_refusals(chart, case):
    config = values()
    for path, value in case["set"].items():
        node = config
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    result = render(chart, config)
    assert result.returncode != 0
    assert "factory-library: " + case["error"] in result.stderr


def generated_config():
    config = values()
    config["secrets"] = {
        "pull": {
            "workload": "app",
            "onePassword": {
                "itemPath": "vaults/factory/items/pull",
                "type": "kubernetes.io/dockerconfigjson",
            },
        },
        "token": {
            "workload": "app",
            "generated": {
                "keys": {"token": 32},
                "image": IMAGE,
                "resources": RESOURCES,
            },
        },
    }
    return config


def test_secrets_are_stable_offline(chart):
    first = render(chart, generated_config())
    second = render(chart, generated_config())
    assert first.stdout == second.stdout
    docs = objects(first)
    container = docs["Job/token-generate"]["spec"]["template"]["spec"]["containers"][0]
    script = container["command"]
    assert script[:2] == ["python3", "-c"]
    compile(script[2], "secret-generator", "exec")
    # Keep the executable script tested below, and the whole manifest golden.
    script[2] = "<secret-generator>"
    golden("secrets", docs)


@pytest.mark.parametrize(
    "existing",
    [{}, {"unrelated": "a2VlcA=="}, {"token": "ZXhpc3Rpbmc=", "unrelated": "a2VlcA=="}],
)
@pytest.mark.parametrize("race", [False, True])
def test_generator_preserves_keys_and_retries_conflicts(
    chart, monkeypatch, existing, race
):
    docs = objects(render(chart, generated_config()))
    job = docs["Job/token-generate"]["spec"]["template"]["spec"]
    script = job["containers"][0]["command"][2]
    for variable in job["containers"][0]["env"]:
        monkeypatch.setenv(variable["name"], variable["value"])
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setattr(Path, "read_text", lambda self: "fixture-token")
    monkeypatch.setattr("ssl.create_default_context", lambda **kwargs: None)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    state = {"metadata": {"resourceVersion": "1"}, "data": copy.deepcopy(existing)}
    writes = []

    def urlopen(request, **kwargs):
        assert request.full_url == (
            "https://10.0.0.1:443/api/v1/namespaces/test/secrets/token"
        )
        if request.get_method() == "PATCH":
            patch = json.loads(request.data)
            assert patch[0] == {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": "1",
            }
            if race:
                state["data"]["token"] = "d2lubmVy"
                state["metadata"]["resourceVersion"] = "2"
                raise urllib.error.HTTPError(
                    request.full_url, 422, "conflict", {}, None
                )
            writes.append(patch)
            state["data"] = patch[1]["value"]
        return io.BytesIO(json.dumps(state).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    exec(compile(script, "secret-generator", "exec"), {})
    for key, value in existing.items():
        assert state["data"][key] == value
    if "token" in existing:
        assert state["data"] == existing
        assert not writes
    elif race:
        assert state["data"]["token"] == "d2lubmVy"
        assert not writes
    else:
        assert len(base64.b64decode(state["data"]["token"])) >= 32
        assert len(writes) == 1


def test_exception_does_not_leak_to_another_workload(chart):
    config = values()
    config["workloads"]["other"] = copy.deepcopy(config["workloads"]["app"])
    config["workloads"]["other"]["network"] = {
        "egress": [{"peer": "world", "port": 443}]
    }
    config["exceptions"] = [
        {"workload": "app", "default": "network.world", "reason": "App only"}
    ]
    result = render(chart, config)
    assert result.returncode != 0
    assert "peer world requires exception network.world" in result.stderr


@pytest.mark.parametrize("kind", ["deployment", "job", "cronjob"])
def test_individual_workload_entry_points(chart, kind):
    (chart / "templates/all.yaml").write_text(
        '{{ include "factory-library.' + kind + '" '
        '(dict "root" . "name" "app" "workload" .Values.workloads.app) }}'
    )
    config = values()
    config["workloads"]["app"].update(replicas=0, backoffLimit=0, schedule="0 * * * *")
    docs = objects(render(chart, config))
    key = {"deployment": "Deployment", "job": "Job", "cronjob": "CronJob"}[kind]
    spec = docs[f"{key}/app"]["spec"]
    if kind == "deployment":
        assert spec["replicas"] == 0
        assert "restartPolicy" not in spec["template"]["spec"]
    else:
        if kind == "cronjob":
            spec = spec["jobTemplate"]["spec"]
        assert spec["backoffLimit"] == 0
        assert spec["template"]["spec"]["restartPolicy"] == "Never"


def test_single_wildcard_requires_and_records_exact_exception(chart):
    config = values()
    config["exceptions"] = [
        {"workload": "app", "default": "network.fqdn:*", "reason": "Legacy proxy"}
    ]
    config["workloads"]["app"]["network"] = {
        "egress": [{"peer": "proxy", "fqdn": "*", "port": 443}]
    }
    docs = objects(render(chart, config))
    assert docs["CiliumNetworkPolicy/app"]["spec"]["egress"][0]["toFQDNs"] == [
        {"matchPattern": "*"}
    ]
