# invariant: PLAT-14
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
    assert (
        "../../projects/platform-factory"
        in _load(cluster / "kustomization.yaml")["resources"]
    )
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


def test_factory_renders_scheduled_jobs_and_app_credential():
    docs = _render()
    assert {(doc["kind"], doc["metadata"]["name"]) for doc in docs} == {
        ("CronJob", "renovate"),
        ("CronJob", "renovate-apko-lock-maintenance"),
        ("ConfigMap", "renovate-apko-lock-maintenance"),
        ("OnePasswordItem", "factory-renovate-gh-app"),
    }
    assert all(doc["metadata"]["namespace"] == "renovate" for doc in docs)
    item = next(doc for doc in docs if doc["kind"] == "OnePasswordItem")
    assert (
        item["spec"]["itemPath"] == "vaults/k8s-homelab/items/factory-renovate-gh-app"
    )
    cron = next(doc for doc in docs if doc["kind"] == "CronJob")
    assert cron["apiVersion"] == "batch/v1"
    spec = cron["spec"]
    assert spec["suspend"] is False
    assert spec["schedule"] == "0 4 * * *"
    assert spec["timeZone"] == "America/Vancouver"
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["startingDeadlineSeconds"] == 3600
    job = spec["jobTemplate"]["spec"]
    assert job["activeDeadlineSeconds"] == 2700
    assert 0 < job["activeDeadlineSeconds"] < 3600
    assert job["backoffLimit"] == 2
    assert job["ttlSecondsAfterFinished"] == 86400
    pod = job["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    values = _load(HERE / "values.yaml")
    assert container["image"] == (
        f"{values['image']['repository']}:{values['image']['tag']}"
    )
    assert container["resources"]["requests"]["memory"] == "4Gi"
    assert container["resources"]["limits"]["memory"] == "4Gi"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert "RENOVATE_TOKEN" not in env
    assert not any("valueFrom" in entry for entry in container["env"])
    for name, value in {
        "RENOVATE_PLATFORM": "github",
        "RENOVATE_REPOSITORIES": "jomcgi-org/homelab",
        "RENOVATE_AUTODISCOVER": "false",
        "RENOVATE_ONBOARDING": "false",
        "RENOVATE_REQUIRE_CONFIG": "required",
        "RENOVATE_ALLOW_SCRIPTS": "false",
        "RENOVATE_ALLOW_PLUGINS": "false",
        "RENOVATE_USERNAME": "jomcgi-factory-renovate[bot]",
        "RENOVATE_PLATFORM_COMMIT": "enabled",
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
    assert mounts == {"workspace": "/tmp", "github-token": "/var/run/renovate-token"}
    assert (
        next(
            mount
            for mount in container["volumeMounts"]
            if mount["name"] == "github-token"
        )["readOnly"]
        is True
    )
    assert "command" not in container  # Keep the image's entrypoint.
    assert container["args"][:2] == ["/bin/sh", "-c"]
    startup = container["args"][2]
    assert 'RENOVATE_TOKEN="$(cat /var/run/renovate-token/token)"' in startup
    assert 'RENOVATE_GIT_AUTHOR="$(cat /var/run/renovate-token/git-author)"' in startup
    assert "export RENOVATE_TOKEN RENOVATE_GIT_AUTHOR" in startup
    assert "exec renovate" in startup

    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert set(volumes) == {"github-app", "github-token", "workspace"}
    assert volumes["workspace"]["emptyDir"] == {"sizeLimit": "4Gi"}
    assert volumes["github-token"]["emptyDir"] == {
        "medium": "Memory",
        "sizeLimit": "1Mi",
    }
    assert volumes["github-app"]["secret"] == {
        "secretName": item["metadata"]["name"],
        "defaultMode": 0o440,
        "items": [
            {"key": name, "path": name}
            for name in ("private-key", "app-id", "installation-id")
        ],
    }


def test_factory_mints_the_token_in_the_renovate_image_before_startup():
    cron = next(doc for doc in _render() if doc["kind"] == "CronJob")
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert len(pod["initContainers"]) == 1
    init = pod["initContainers"][0]
    assert init["name"] == "github-app-token"
    assert init["image"] == pod["containers"][0]["image"]
    assert init["imagePullPolicy"] == pod["containers"][0]["imagePullPolicy"]
    assert init["command"] == ["node", "--input-type=module", "--eval"]
    assert (
        init["args"][0].strip()
        == (HERE / "files/github-app-token.mjs").read_text().strip()
    )
    assert {entry["name"]: entry["value"] for entry in init["env"]} == {
        "GITHUB_APP_CREDENTIALS_DIR": "/var/run/renovate-github-app",
        "RENOVATE_TOKEN_DIR": "/var/run/renovate-token",
        "RENOVATE_USERNAME": "jomcgi-factory-renovate[bot]",
    }
    assert init["volumeMounts"] == [
        {
            "name": "github-app",
            "mountPath": "/var/run/renovate-github-app",
            "readOnly": True,
        },
        {"name": "github-token", "mountPath": "/var/run/renovate-token"},
    ]
    assert init["securityContext"]["readOnlyRootFilesystem"] is True
    assert init["securityContext"]["runAsNonRoot"] is True
    assert init["securityContext"]["allowPrivilegeEscalation"] is False
    assert init["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert init["resources"]["requests"] == {"cpu": "100m", "memory": "64Mi"}
    assert init["resources"]["limits"]["memory"] == "128Mi"


def test_factory_weekly_apko_uses_the_app_and_requires_review():
    docs = _render()
    weekly = next(
        d for d in docs if d["kind"] == "CronJob" and "apko" in d["metadata"]["name"]
    )
    spec = weekly["spec"]
    assert spec["schedule"] == "0 1 * * 1"
    assert spec["timeZone"] == "America/Vancouver"
    assert spec["suspend"] is False
    assert spec["concurrencyPolicy"] == "Forbid"
    job = spec["jobTemplate"]["spec"]
    assert 0 < job["activeDeadlineSeconds"] < 3600
    pod = job["template"]["spec"]
    assert pod["initContainers"][0]["name"] == "github-app-token"
    container = pod["containers"][0]
    assert {m["name"] for m in container["volumeMounts"]} == {
        "github-token",
        "workspace",
        "script",
    }
    assert container["args"] == ["/bin/bash", "/opt/renovate-apko/maintain.sh"]
    assert "MAINTENANCE_AUTOMERGE_ENABLED" not in {e["name"] for e in container["env"]}
    script = next(d["data"]["maintain.sh"] for d in docs if d["kind"] == "ConfigMap")
    assert script.strip().endswith("maintain apko")


def test_factory_token_check_accepts_current_installation_token_formats():
    script = (HERE / "files/github-app-token.mjs").read_text()
    shape = re.search(r"!/(\^[^/]+\$)/\.test\(token\)", script)
    assert shape, "token shape check not found"
    accepts = re.compile(shape.group(1))
    # Synthetic tokens: the classic 40-character form and the longer dotted
    # form GitHub issued on 2026-11-15 (390 characters with "." and "-").
    classic = "ghs_" + "A1b2C3d4" * 4 + "Ab12"
    dotted = "ghs_" + ".".join(["eyJ0eXAi-OiJKV1Qi_" * 7] * 3)
    for token in (classic, dotted):
        assert accepts.fullmatch(token), token[:12]
    for token in ("", "ghs_a b", "ghs_a\nb", 'ghs_a"b', "ghs_a/b", "ghs_a$b"):
        assert not accepts.fullmatch(token), repr(token)


def test_factory_refuses_jobs_that_can_outlive_an_installation_token():
    for deadline in (0, 3600, 7200):
        result = subprocess.run(
            [
                os.environ.get("HELM_BIN", "helm"),
                "template",
                "renovate",
                str(HERE),
                "--values",
                str(HERE / "values-factory.yaml"),
                "--set",
                f"activeDeadlineSeconds={deadline}",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode != 0
        assert "activeDeadlineSeconds between 1 and 3599" in result.stderr


def test_factory_egress_matches_the_job_and_keeps_other_traffic_denied():
    policy = _load(DEPLOY / "resources/network-policy.yaml")
    assert policy["metadata"]["namespace"] == _application()["destination"]["namespace"]
    assert int(policy["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]) < 0
    spec = policy["spec"]
    cron = next(doc for doc in _render() if doc["kind"] == "CronJob")
    # Init containers share the selected pod endpoint and its egress policy.
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["initContainers"][0]["name"] == "github-app-token"
    assert pod.get("hostNetwork", False) is False
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
