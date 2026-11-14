"""Verify factory enrollment, the rendered Job and its default-deny boundary."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
FACTORY = ROOT / "projects/platform-factory"
DEPLOY = FACTORY / "renovate"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _application() -> dict:
    return _load(DEPLOY / "application.yaml")["spec"]


def _render() -> list[dict]:
    application = _application()
    source = application["sources"][0]
    command = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        source["helm"]["releaseName"],
        str(ROOT / source["path"]),
        "--namespace",
        application["destination"]["namespace"],
    ]
    for values in source["helm"]["valueFiles"]:
        command.extend(["--values", str(HERE / values)])
    rendered = subprocess.run(
        command, capture_output=True, text=True, timeout=120, check=True
    )
    return [doc for doc in yaml.safe_load_all(rendered.stdout) if doc]


def test_factory_root_reaches_the_shared_chart_and_namespace_policy():
    cluster = ROOT / "projects/factory-cluster"
    parent = _load(cluster / "factory-root-application.yaml")
    assert parent["spec"]["source"]["path"] == "projects/factory-cluster/root"
    root = _load(cluster / "root/root-application.yaml")
    assert root["spec"]["source"]["path"] == "projects/factory-cluster"
    assert "../../projects/platform-factory" in _load(cluster / "kustomization.yaml")[
        "resources"
    ]
    assert "./renovate" in _load(FACTORY / "kustomization.yaml")["resources"]
    assert _load(DEPLOY / "kustomization.yaml")["resources"] == ["application.yaml"]
    application = _application()
    assert application["destination"] == {
        "server": "https://kubernetes.default.svc",
        "namespace": "renovate",
    }
    chart, policy = application["sources"]
    assert chart["path"] == "projects/platform/renovate"
    assert chart["helm"] == {
        "releaseName": "renovate",
        "valueFiles": ["values.yaml", "values-factory.yaml"],
    }
    assert policy["path"] == "projects/platform-factory/renovate/resources"
    for source in application["sources"]:
        assert source["repoURL"] == "https://github.com/jomcgi-org/homelab.git"
        assert source["targetRevision"] == "HEAD"
    assert "CreateNamespace=true" in application["syncPolicy"]["syncOptions"]
    assert _load(DEPLOY / "resources/kustomization.yaml")["resources"] == [
        "network-policy.yaml"
    ]


def test_factory_renders_only_a_suspended_job_and_existing_credential():
    docs = _render()
    assert {(doc["kind"], doc["metadata"]["name"]) for doc in docs} == {
        ("CronJob", "renovate"),
        ("OnePasswordItem", "renovate-github"),
    }
    assert all(doc["metadata"]["namespace"] == "renovate" for doc in docs)
    item = next(doc for doc in docs if doc["kind"] == "OnePasswordItem")
    assert item["spec"]["itemPath"] == "vaults/k8s-homelab/items/renovate-github"
    cron = next(doc for doc in docs if doc["kind"] == "CronJob")
    assert cron["apiVersion"] == "batch/v1"
    spec = cron["spec"]
    assert spec["suspend"] is True
    assert spec["schedule"] == "0 4 * * *"
    assert spec["timeZone"] == "America/Vancouver"
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["startingDeadlineSeconds"] == 3600
    job = spec["jobTemplate"]["spec"]
    assert job["activeDeadlineSeconds"] == 7200
    assert job["backoffLimit"] == 2
    assert job["ttlSecondsAfterFinished"] == 86400
    pod = job["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    values = _load(HERE / "values.yaml")
    assert container["image"] == (
        f'{values["image"]["repository"]}:{values["image"]["tag"]}'
    )
    assert container["resources"]["requests"]["memory"] == "4Gi"
    assert container["resources"]["limits"]["memory"] == "4Gi"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["RENOVATE_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": item["metadata"]["name"],
        "key": "RENOVATE_TOKEN",
    }
    for name, value in {
        "RENOVATE_PLATFORM": "github",
        "RENOVATE_REPOSITORIES": "jomcgi-org/homelab",
        "RENOVATE_AUTODISCOVER": "false",
        "RENOVATE_ONBOARDING": "false",
        "RENOVATE_REQUIRE_CONFIG": "required",
        "RENOVATE_ALLOW_SCRIPTS": "false",
        "RENOVATE_ALLOW_PLUGINS": "false",
    }.items():
        assert env[name]["value"] == value
    assert json.loads(env["RENOVATE_CUSTOM_ENV_VARIABLES"]["value"]) == {
        "UV_NO_BUILD": "true"
    }
    commands = json.loads(env["RENOVATE_ALLOWED_COMMANDS"]["value"])
    assert len(commands) == 1
    command = "bazel/tools/format/update-python-requirements.sh"
    assert re.fullmatch(commands[0], command)
    for unsafe in [command + "; id", command + " extra", command.replace(".sh", "Xsh")]:
        assert not re.fullmatch(commands[0], unsafe)
    assert json.loads(env["RENOVATE_ALLOWED_UNSAFE_EXECUTIONS"]["value"]) == [
        "bazelModDeps"
    ]
    mounts = {mount["name"]: mount["mountPath"] for mount in container["volumeMounts"]}
    assert mounts == {"workspace": "/tmp"}
    assert pod["volumes"] == [{"name": "workspace", "emptyDir": {"sizeLimit": "4Gi"}}]


def test_factory_egress_matches_the_job_and_keeps_other_traffic_denied():
    policy = _load(DEPLOY / "resources/network-policy.yaml")
    assert policy["metadata"]["namespace"] == _application()["destination"]["namespace"]
    assert int(policy["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]) < 0
    spec = policy["spec"]
    cron = next(doc for doc in _render() if doc["kind"] == "CronJob")
    pod_labels = cron["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert spec["endpointSelector"]["matchLabels"]
    for key, value in spec["endpointSelector"]["matchLabels"].items():
        assert pod_labels[key] == value
    assert not spec.get("ingress")
    names = set()
    patterns = set()
    for rule in spec["egress"]:
        assert set(rule) == {"toFQDNs", "toPorts"}
        assert rule["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
        for fqdn in rule["toFQDNs"]:
            if "matchName" in fqdn:
                names.add(fqdn["matchName"])
            else:
                patterns.add(fqdn["matchPattern"])
    assert patterns == {"*.githubusercontent.com", "*.quay.io", "*-docker.pkg.dev"}
    assert all("*" not in name for name in names)
    assert {
        "github.com",
        "api.github.com",
        "registry.npmjs.org",
        "pypi.org",
        "files.pythonhosted.org",
        "proxy.golang.org",
        "sum.golang.org",
        "storage.googleapis.com",
        "ghcr.io",
        "registry-1.docker.io",
        "auth.docker.io",
        "cgr.dev",
        "registry.k8s.io",
        "bcr.bazel.build",
        "get.helm.sh",
        "helm.cilium.io",
        "cloudnative-pg.github.io",
        "1password.github.io",
        "argoproj.github.io",
        "charts.goauthentik.io",
        "kyverno.github.io",
        "pkgs.tailscale.com",
        "spiffe.github.io",
    } <= names
