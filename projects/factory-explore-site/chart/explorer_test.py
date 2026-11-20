import os
from pathlib import Path
import subprocess

import yaml


DIGEST = "sha256:" + "0" * 64


def render(*settings):
    rendered = subprocess.check_output(
        [
            os.environ["HELM_BIN"],
            "template",
            "factory-explore",
            str(Path(__file__).parent),
            "--namespace",
            "factory-explore-site",
            "--set",
            f"image.digest={DIGEST}",
            *settings,
        ],
        text=True,
    )
    resources = [doc for doc in yaml.safe_load_all(rendered) if doc]
    return resources


def test_registered_release_waits_for_activation():
    resources = render()
    assert sorted(
        d["kind"]
        for d in resources
        if d
        and d["metadata"].get("namespace") == "factory-explore-site"
        and d["kind"] in {"ResourceQuota", "LimitRange"}
    ) == ["LimitRange", "ResourceQuota"]
    resources = [
        doc for doc in resources if doc["kind"] not in {"ResourceQuota", "LimitRange"}
    ]
    assert [doc["kind"] for doc in resources] == ["ConfigMap"]
    assert "publishEnabled" not in resources[0]["metadata"]
    assert resources[0]["data"]["publishEnabled"] == "false"


def test_worker_deploy_job():
    resources = render("--set", "publishEnabled=true")
    marker = next(
        doc
        for doc in resources
        if doc["kind"] == "ConfigMap"
        and doc["metadata"]["name"] == "factory-explore-release"
    )
    assert "publishEnabled" not in marker["metadata"]
    assert marker["data"]["publishEnabled"] == "true"
    jobs = [doc for doc in resources if doc["kind"] == "Job"]
    assert len(jobs) == 1
    assert jobs[0]["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
    container = jobs[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].endswith("@" + DIGEST)
    assert "wrangler deploy" in container["command"][-1]
    assert {"Service", "Deployment", "HTTPRoute"}.isdisjoint(
        doc["kind"] for doc in resources
    )
    cloudflare = {
        entry["valueFrom"]["secretKeyRef"]["name"]
        for entry in container["env"]
        if entry["name"].startswith("CLOUDFLARE_")
    }
    assert cloudflare == {"factory-explore-cloudflare"}
    assert "jomcgi-site-cloudflare" not in str(resources)


def test_reverting_publication_withdraws_managed_hostname():
    resources = render("--set", "manageHostname=true")
    (job,) = [doc for doc in resources if doc["kind"] == "Job"]
    (container,) = job["spec"]["template"]["spec"]["containers"]
    assert container["command"] == ["/app/projects/factory-explore-site/withdraw"]


def test_managed_publish_uses_release_image_over_rollback_image():
    fallback = "sha256:" + "1" * 64
    resources = render(
        "--set",
        "manageHostname=true,publishEnabled=true",
        "--set",
        "workloads.factory-explore-deploy.command={/app/projects/factory-explore-site/withdraw}",
        "--set",
        "workloads.factory-explore-deploy.image.repository=example.com/rollback",
        "--set",
        f"workloads.factory-explore-deploy.image.digest={fallback}",
    )
    (job,) = [doc for doc in resources if doc["kind"] == "Job"]
    (container,) = job["spec"]["template"]["spec"]["containers"]
    assert container["image"].endswith("@" + DIGEST)
    assert "wrangler deploy" in container["command"][-1]
