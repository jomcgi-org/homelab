"""Render the deploy Job, its release marker and its network boundary."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml

DIGEST = "sha256:" + "0" * 64
CHART = Path(__file__).resolve().parent


def render(*extra):
    return subprocess.run(
        [
            os.environ["HELM_BIN"],
            "template",
            "jomcgi-site-dev",
            str(CHART),
            "--namespace",
            "jomcgi-site-dev",
            "--set",
            f"image.digest={DIGEST}",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def docs(result):
    assert result.returncode == 0, result.stderr
    return {
        f"{doc['kind']}/{doc['metadata']['name']}": doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }


def test_deploy_job_runs_on_every_sync_against_the_dev_worker():
    rendered = docs(render())
    job = rendered["Job/jomcgi-site-deploy"]
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == [
        "deploy",
        "--config",
        "/app/site/wrangler.jsonc",
        "--env",
        "dev",
    ]
    assert container["image"].endswith(f"@{DIGEST}")
    secrets = {
        env["name"]: env["valueFrom"]["secretKeyRef"]
        for env in container["env"]
        if "valueFrom" in env
    }
    assert secrets == {
        "CLOUDFLARE_API_TOKEN": {"name": "jomcgi-site-cloudflare", "key": "api-token"},
        "CLOUDFLARE_ACCOUNT_ID": {
            "name": "jomcgi-site-cloudflare",
            "key": "account-id",
        },
    }
    assert (
        rendered["OnePasswordItem/jomcgi-site-cloudflare"]["spec"]["itemPath"]
        == "vaults/k8s-homelab/items/jomcgi-site-cloudflare"
    )


def test_a_new_digest_changes_the_release_marker():
    other = "sha256:" + "1" * 64
    first = docs(render())["ConfigMap/jomcgi-site-release"]["data"]["image"]
    second = docs(render("--set", f"image.digest={other}"))[
        "ConfigMap/jomcgi-site-release"
    ]["data"]["image"]
    assert first.endswith(f"@{DIGEST}")
    assert second.endswith(f"@{other}")
    assert first != second


def test_release_marker_changes_with_the_image():
    # Argo CD does not diff hooks; this ConfigMap is what makes a new image a sync.
    marker = docs(render())["ConfigMap/jomcgi-site-release"]["data"]
    assert marker == {
        "stage": "dev",
        "image": f"ghcr.io/jomcgi/homelab/projects/jomcgi-site@{DIGEST}",
    }


def test_only_egress_is_the_cloudflare_api():
    policy = docs(render())["CiliumNetworkPolicy/jomcgi-site-deploy"]["spec"]
    assert policy["enableDefaultDeny"] == {"ingress": True, "egress": True}
    assert policy.get("ingress", []) == []
    assert policy["egress"] == [
        {
            "toFQDNs": [{"matchName": "api.cloudflare.com"}],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        }
    ]


@pytest.mark.parametrize(
    "extra, error",
    [
        (["--set", "stage=staging"], "stage must be dev or prod"),
        (["--set", "stage=prod"], "--env argument must match .Values.stage"),
    ],
)
def test_stage_guard(extra, error):
    result = render(*extra)
    assert result.returncode != 0
    assert error in result.stderr
