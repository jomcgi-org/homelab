"""Rendered-chart guard for the public FaaS component token boundary."""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import yaml

AUDIENCE = "embervm-public-faas"
MOUNT_PATH = "/var/run/secrets/embervm-public-faas"
TOKEN_FILE = f"{MOUNT_PATH}/token"
VOLUME_NAME = "embervm-public-faas-token"


def _chart_dir() -> Path:
    local = Path(__file__).resolve().parent.parent / "monolith-public" / "chart"
    if (local / "Chart.yaml").exists():
        return local
    srcdir = Path(os.environ.get("TEST_SRCDIR", ""))
    candidate = srcdir / "_main" / "projects" / "monolith-public" / "chart"
    if (candidate / "Chart.yaml").exists():
        return candidate
    raise FileNotFoundError(
        f"monolith-public chart not found at {local} or {candidate}"
    )


def _value_file(env_name: str, relative: str) -> Path:
    configured = os.environ.get(env_name)
    if configured:
        return Path(configured)
    path = Path(__file__).resolve().parents[1] / "monolith-public" / "deploy" / relative
    if path.exists():
        return path
    raise FileNotFoundError(f"{relative} not found and {env_name} is unset")


def _render(overlays=("DEPLOY_VALUES", "GKE_VALUES")) -> list[dict]:
    chart = _chart_dir()
    value_files = {
        "DEPLOY_VALUES": "values.yaml",
        "GKE_VALUES": "values-gke.yaml",
    }
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith-public",
            str(chart),
            "--namespace",
            "monolith-public",
            "--values",
            str(chart / "values.yaml"),
            *[
                arg
                for name in overlays
                for arg in ("--values", str(_value_file(name, value_files[name])))
            ],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc is not None]


def _one(docs: list[dict], kind: str, suffix: str) -> dict:
    matches = [
        doc
        for doc in docs
        if doc.get("kind") == kind
        and doc.get("metadata", {}).get("name", "").endswith(suffix)
    ]
    assert len(matches) == 1, f"expected one {kind} named *{suffix}, got {len(matches)}"
    return matches[0]


def _service_account_token_sources(pod_spec: dict) -> list[dict]:
    return [
        source["serviceAccountToken"]
        for volume in pod_spec.get("volumes", [])
        for source in volume.get("projected", {}).get("sources", [])
        if "serviceAccountToken" in source
    ]


def test_public_web_gets_only_the_component_audience_token():
    docs = _render()
    service_account = _one(docs, "ServiceAccount", "monolith-public")
    assert service_account["automountServiceAccountToken"] is False

    web = _one(docs, "Deployment", "-web")
    pod_spec = web["spec"]["template"]["spec"]
    assert _service_account_token_sources(pod_spec) == [
        {"audience": AUDIENCE, "expirationSeconds": 3600, "path": "token"}
    ]

    volumes = {item["name"]: item for item in pod_spec["volumes"]}
    assert volumes[VOLUME_NAME]["projected"]["defaultMode"] == 0o440

    container = pod_spec["containers"][0]
    mounts = {item["name"]: item for item in container["volumeMounts"]}
    assert mounts[VOLUME_NAME] == {
        "name": VOLUME_NAME,
        "mountPath": MOUNT_PATH,
        "readOnly": True,
    }
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["K8S_AUTH_TOKEN_FILE"] == TOKEN_FILE

    frontend = _one(docs, "Deployment", "-frontend")
    assert _service_account_token_sources(frontend["spec"]["template"]["spec"]) == []


def test_public_service_account_has_no_rendered_rbac_grant():
    docs = _render()
    rbac_kinds = {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}
    assert [doc for doc in docs if doc.get("kind") in rbac_kinds] == []


def test_public_functions_route_still_targets_the_web_component():
    docs = _render()
    route = _one(docs, "HTTPRoute", "-functions")
    rule = route["spec"]["rules"][0]
    assert rule["matches"] == [{"path": {"type": "PathPrefix", "value": "/functions/"}}]
    assert rule["backendRefs"][0]["name"].endswith("-web")


def test_public_web_renders_otel_traces_endpoint():
    deployments = [doc for doc in _render() if doc.get("kind") == "Deployment"]
    web = [
        doc
        for doc in deployments
        if doc["spec"]["template"]["metadata"]["labels"].get(
            "app.kubernetes.io/component"
        )
        == "web"
    ]
    assert len(web) == 1
    container = web[0]["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    endpoint = env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
    assert endpoint.endswith(":4318/v1/traces")


def test_public_retrieval_env_matches_reader_in_every_layer():
    reader = Path(__file__).resolve().parent / "grimoire_chat" / "retrieval.py"
    tree = ast.parse(reader.read_text())
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "RETRIEVAL_K" for t in node.targets)
    )
    lookup = assignment.value.args[0]
    assert ast.unparse(lookup.func) == "os.environ.get"
    name = ast.literal_eval(lookup.args[0])
    assert name == "GRIMOIRE_CHAT_RETRIEVAL_K"

    for overlays in ((), ("DEPLOY_VALUES",), ("DEPLOY_VALUES", "GKE_VALUES")):
        docs = _render(overlays)
        web = _one(docs, "Deployment", "-web")
        entries = web["spec"]["template"]["spec"]["containers"][0]["env"]
        env = {item["name"]: item for item in entries}
        assert len(env) == len(entries), "duplicate environment variable"
        assert env[name] == {"name": name, "value": "6"}
        assert "CHAT_PUBLIC_RETRIEVAL_K" not in env


def test_public_gke_preserves_complete_env_and_secret_boundaries():
    defaults = yaml.safe_load((_chart_dir() / "values.yaml").read_text())
    base = yaml.safe_load(_value_file("DEPLOY_VALUES", "values.yaml").read_text())
    gke = yaml.safe_load(_value_file("GKE_VALUES", "values-gke.yaml").read_text())
    expected = {item["name"]: item for item in defaults["web"]["env"]}
    expected["CHAT_PUBLIC_INFERENCE_URL"] = {
        "name": "CHAT_PUBLIC_INFERENCE_URL",
        "value": "https://api.meta.ai",
    }
    expected["META_SPARK_API_KEY"]["valueFrom"]["secretKeyRef"].pop("optional")
    assert {item["name"]: item for item in gke["web"]["env"]} == expected
    expected["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = {
        "name": "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "value": gke["otelEndpoint"],
    }
    docs = _render()
    web = _one(docs, "Deployment", "-web")
    container = web["spec"]["template"]["spec"]["containers"][0]
    assert {item["name"]: item for item in container["env"]} == expected
    frontend = _one(docs, "Deployment", "-frontend")
    frontend_env = frontend["spec"]["template"]["spec"]["containers"][0]["env"]
    assert frontend_env == defaults["frontend"]["env"]
    for component in (web, frontend):
        env = component["spec"]["template"]["spec"]["containers"][0]["env"]
        assert len(env) == len({item["name"] for item in env})
    turnstile = _one(docs, "OnePasswordItem", "-turnstile")
    assert turnstile["spec"]["itemPath"] == base["turnstile"]["onepassword"]["itemPath"]
    for route in (doc for doc in docs if doc.get("kind") == "HTTPRoute"):
        for rule in route["spec"]["rules"]:
            for match in rule.get("matches", []):
                assert not match.get("path", {}).get("value", "").startswith("/api")
