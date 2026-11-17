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


def test_wrangler_can_write_its_state_dirs():
    # wrangler writes HOME and .wrangler/tmp next to wrangler.jsonc; the root is read-only.
    pod = docs(render())["Job/jomcgi-site-deploy"]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    mounts = {m["mountPath"] for m in container["volumeMounts"]}
    assert {"/tmp", "/app/site/.wrangler"} <= mounts


def test_a_new_digest_changes_the_release_marker():
    other = "sha256:" + "1" * 64
    first = docs(render())["ConfigMap/jomcgi-site-release"]["data"]["image"]
    second = docs(render("--set", f"image.digest={other}"))[
        "ConfigMap/jomcgi-site-release"
    ]["data"]["image"]
    assert first.endswith(f"@{DIGEST}")
    assert second.endswith(f"@{other}")
    assert first != second


def test_release_marker_changes_with_the_chart_and_the_image():
    # Argo CD does not diff hooks; this ConfigMap is what makes a new chart or
    # image a sync. The chart version covers a change to the Job alone.
    version = yaml.safe_load((CHART / "Chart.yaml").read_text())["version"]
    marker = docs(render())["ConfigMap/jomcgi-site-release"]["data"]
    assert marker == {
        "chart": version,
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


PROD = ("--set", "stage=prod", "--set", "edge.enabled=true")


def test_prod_deploys_the_prod_worker_and_applies_the_edge_config():
    rendered = docs(render(*PROD))
    deploy = rendered["Job/jomcgi-site-deploy"]["spec"]["template"]["spec"]
    assert deploy["containers"][0]["args"][-2:] == ["--env", "prod"]
    job = rendered["Job/jomcgi-site-edge"]
    # PostSync: after the deploy Job, so every route's Worker exists.
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PostSync"
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["/app/projects/jomcgi-site/edge/apply"]
    assert container["image"].endswith(f"@{DIGEST}")
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert [env["name"] for env in container["env"]] == ["CLOUDFLARE_API_TOKEN"]
    policy = rendered["CiliumNetworkPolicy/jomcgi-site-edge"]["spec"]
    assert policy["egress"] == [
        {
            "toFQDNs": [{"matchName": "api.cloudflare.com"}],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        }
    ]
    assert rendered["ConfigMap/jomcgi-site-release"]["data"]["stage"] == "prod"


def test_dev_has_no_edge_job():
    rendered = docs(render())
    assert "Job/jomcgi-site-edge" not in rendered
    assert not any(name.endswith("jomcgi-site-edge") for name in rendered)


@pytest.mark.parametrize(
    "extra, error",
    [
        (["--set", "stage=staging"], "stage must be dev or prod"),
        (["--set", "edge.enabled=true"], "edge.enabled is for the prod stage only"),
        (
            ["--set", "workloads.jomcgi-site-deploy.args[3]=--env"],
            "--env comes from .Values.stage",
        ),
    ],
)
def test_stage_guard(extra, error):
    result = render(*extra)
    assert result.returncode != 0
    assert error in result.stderr
