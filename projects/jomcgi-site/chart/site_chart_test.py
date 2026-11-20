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
    assert sorted(
        d["kind"]
        for d in rendered.values()
        if d
        and d["metadata"].get("namespace") == "jomcgi-site-dev"
        and d["kind"] in {"ResourceQuota", "LimitRange"}
    ) == ["LimitRange", "ResourceQuota"]
    job = rendered["Job/jomcgi-site-deploy"]
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
    container = job["spec"]["template"]["spec"]["containers"][0]
    # run.sh: data snapshot into a /tmp copy, then `wrangler deploy --env $STAGE`.
    assert container["command"] == ["/bin/bash", "/app/site/run.sh"]
    assert "args" not in container
    assert {"name": "STAGE", "value": "dev"} in container["env"]
    assert {"name": "MODE", "value": "sync"} in container["env"]
    version = yaml.safe_load((CHART / "Chart.yaml").read_text())["version"]
    # The release label: chart version and the image digest's first 12 hex.
    assert {
        "name": "RELEASE",
        "value": f"chart {version} image {DIGEST.removeprefix('sha256:')[:12]}",
    } in container["env"]
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
        "R2_ACCESS_KEY_ID": {"name": "jomcgi-site-r2-read", "key": "access-key-id"},
        "R2_SECRET_ACCESS_KEY": {
            "name": "jomcgi-site-r2-read",
            "key": "secret-access-key",
        },
    }
    assert (
        rendered["OnePasswordItem/jomcgi-site-cloudflare"]["spec"]["itemPath"]
        == "vaults/k8s-homelab/items/jomcgi-site-cloudflare"
    )
    assert (
        rendered["OnePasswordItem/jomcgi-site-r2-read"]["spec"]["itemPath"]
        == "vaults/k8s-homelab/items/r2-s3-public-read"
    )


def test_refresh_cronjob_runs_the_deploy_command_on_a_schedule():
    rendered = docs(render())
    deploy = rendered["Job/jomcgi-site-deploy"]["spec"]["template"]["spec"][
        "containers"
    ][0]
    cron = rendered["CronJob/jomcgi-site-refresh"]
    assert cron["spec"]["schedule"] == "*/30 * * * *"
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    # Not a hook: it runs on its schedule, not on syncs.
    assert "argocd.argoproj.io/hook" not in cron["metadata"].get("annotations", {})
    container = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == deploy["command"]
    assert container["image"] == deploy["image"]
    # The same env but MODE: run.sh's fence keeps a refresh from replacing a
    # newer release, and the hook re-checks after its deploy.
    without_mode = lambda env: [e for e in env if e["name"] != "MODE"]
    assert without_mode(container["env"]) == without_mode(deploy["env"])
    assert {"name": "MODE", "value": "refresh"} in container["env"]
    prod = docs(render(*PROD))["CronJob/jomcgi-site-refresh"]
    prod_env = prod["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0][
        "env"
    ]
    assert {"name": "STAGE", "value": "prod"} in prod_env
    assert "CronJob/jomcgi-site-refresh" not in docs(
        render("--set", "refresh.enabled=false")
    )


def test_the_site_copy_and_wrangler_state_live_in_tmp():
    # run.sh copies the site to /tmp/site, so wrangler's .wrangler/tmp (next
    # to the config) and HOME are under /tmp; the root stays read-only.
    pod = docs(render())["Job/jomcgi-site-deploy"]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert {m["mountPath"] for m in container["volumeMounts"]} == {"/tmp"}
    tmp = next(v for v in pod["volumes"] if v["name"] == "tmp")
    assert tmp["emptyDir"]["sizeLimit"] == "4Gi"


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
        "edge": "false",
        "serveRoutes": "false",
        "image": f"ghcr.io/jomcgi/homelab/projects/jomcgi-site@{DIGEST}",
    }


@pytest.mark.parametrize("workload", ["jomcgi-site-deploy", "jomcgi-site-refresh"])
def test_deploy_egress(workload):
    policy = docs(render())[f"CiliumNetworkPolicy/{workload}"]["spec"]
    assert policy["enableDefaultDeny"] == {"ingress": True, "egress": True}
    assert policy.get("ingress", []) == []
    https = {"toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}
    assert policy["egress"] == [
        {"toFQDNs": [{"matchName": "api.cloudflare.com"}], **https},
        {
            "toFQDNs": [
                {
                    "matchName": "7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com"
                }
            ],
            **https,
        },
        {"toFQDNs": [{"matchName": "factory-data.jomcgi.dev"}], **https},
    ]


PROD = ("--set", "stage=prod", "--set", "edge.enabled=true")


def test_prod_deploys_the_prod_worker_and_applies_the_edge_config():
    rendered = docs(render(*PROD))
    deploy = rendered["Job/jomcgi-site-deploy"]["spec"]["template"]["spec"]
    assert {"name": "STAGE", "value": "prod"} in deploy["containers"][0]["env"]
    job = rendered["Job/jomcgi-site-edge"]
    # PostSync: after the deploy Job, so every route's Worker exists.
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PostSync"
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["/app/projects/jomcgi-site/edge/apply"]
    assert container["image"].endswith(f"@{DIGEST}")
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert [env["name"] for env in container["env"]] == [
        "CLOUDFLARE_API_TOKEN",
        "EDGE_SERVE_ROUTES",
    ]
    # Routes stay off until the prod Application turns them on.
    assert container["env"][1]["value"] == "false"
    policy = rendered["CiliumNetworkPolicy/jomcgi-site-edge"]["spec"]
    assert policy["egress"] == [
        {
            "toFQDNs": [{"matchName": "api.cloudflare.com"}],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        }
    ]
    assert rendered["ConfigMap/jomcgi-site-release"]["data"]["stage"] == "prod"


def test_serve_routes_turns_the_routes_on():
    rendered = docs(render(*PROD, "--set", "edge.serveRoutes=true"))
    env = rendered["Job/jomcgi-site-edge"]["spec"]["template"]["spec"]["containers"][0][
        "env"
    ]
    assert {"name": "EDGE_SERVE_ROUTES", "value": "true"} in env


def test_toggling_the_route_switch_changes_the_release_marker():
    # Argo CD does not diff the edge hook, so the switch must change a
    # non-hook resource, or a values-only PR (and its revert) would not sync.
    off = docs(render(*PROD))["ConfigMap/jomcgi-site-release"]["data"]
    on = docs(render(*PROD, "--set", "edge.serveRoutes=true"))[
        "ConfigMap/jomcgi-site-release"
    ]["data"]
    assert (off["edge"], off["serveRoutes"]) == ("true", "false")
    assert (on["edge"], on["serveRoutes"]) == ("true", "true")
    assert {k: v for k, v in off.items() if k != "serveRoutes"} == {
        k: v for k, v in on.items() if k != "serveRoutes"
    }


def test_dev_has_no_edge_job():
    rendered = docs(render())
    assert "Job/jomcgi-site-edge" not in rendered
    assert not any(name.endswith("jomcgi-site-edge") for name in rendered)


def test_site_check_is_a_restricted_postsync_hook_with_no_deploy_credentials():
    rendered = docs(render())
    job = rendered["Job/site-check"]
    assert job["metadata"]["annotations"] == {
        "argocd.argoproj.io/hook": "PostSync",
        "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation,HookSucceeded",
        "argocd.argoproj.io/sync-wave": "1",
    }
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    assert container["image"].endswith(f"@{DIGEST}")
    assert container["command"] == ["/app/projects/jomcgi-site/check/site-check"]
    assert container["args"] == ["dev"]
    assert container["env"] == [
        {"name": "HOME", "value": "/tmp"},
        {"name": "TMPDIR", "value": "/tmp"},
    ]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    policy = rendered["CiliumNetworkPolicy/site-check"]["spec"]
    assert policy["endpointSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "site-check"
    }
    assert policy["enableDefaultDeny"] == {"ingress": True, "egress": True}
    assert policy.get("ingress", []) == []
    assert policy["egress"] == [
        {
            "toFQDNs": [{"matchName": host}],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        }
        for host in ["next.jomcgi.dev", "tiles.openfreemap.org"]
    ]


def test_prod_checks_its_own_host():
    rendered = docs(render(*PROD, "--set", "edge.serveRoutes=true"))
    assert rendered["Job/site-check"]["spec"]["template"]["spec"]["containers"][0][
        "args"
    ] == ["prod"]
    assert rendered["CiliumNetworkPolicy/site-check"]["spec"]["egress"][0][
        "toFQDNs"
    ] == [{"matchName": "jomcgi.dev"}]
    edge = rendered["Job/jomcgi-site-edge"]["metadata"]["annotations"]
    check = rendered["Job/site-check"]["metadata"]["annotations"]
    assert edge["argocd.argoproj.io/hook"] == check["argocd.argoproj.io/hook"]
    assert int(check["argocd.argoproj.io/sync-wave"]) > int(
        edge.get("argocd.argoproj.io/sync-wave", "0")
    )


def test_prod_with_routes_off_does_not_audit_hub_pages():
    rendered = docs(render(*PROD))
    assert not any(name.endswith("/site-check") for name in rendered)
    assert "Job/jomcgi-site-deploy" in rendered
    # The edge Job still runs, so it deletes every route before the hub serves prod.
    edge = rendered["Job/jomcgi-site-edge"]["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "EDGE_SERVE_ROUTES", "value": "false"} in edge["env"]


@pytest.mark.parametrize("serve_routes", ["true", "false"])
def test_prod_never_skips_both_the_edge_job_and_site_check(serve_routes):
    # Routes from an earlier sync can be live, so prod without the edge Job,
    # which alone deletes them, is refused rather than left unchecked.
    result = render(
        "--set", f"stage=prod,edge.enabled=false,edge.serveRoutes={serve_routes}"
    )
    assert result.returncode != 0
    assert "the prod stage needs edge.enabled" in result.stderr


@pytest.mark.parametrize("serve_routes", ["true", "false"])
def test_dev_check_does_not_depend_on_prod_routes(serve_routes):
    rendered = docs(render("--set", f"edge.serveRoutes={serve_routes}"))
    assert rendered["Job/site-check"]["spec"]["template"]["spec"]["containers"][0][
        "args"
    ] == ["dev"]


@pytest.mark.parametrize(
    "extra, error",
    [
        (["--set", "stage=staging"], "stage must be dev or prod"),
        (["--set", "edge.enabled=true"], "edge.enabled is for the prod stage only"),
        (
            ["--set", "workloads.jomcgi-site-deploy.env[0].name=STAGE"],
            "STAGE comes from the chart",
        ),
        (
            ["--set", "workloads.jomcgi-site-deploy.env[0].name=MODE"],
            "MODE comes from the chart",
        ),
        (
            ["--set", "workloads.site-check.args[0]=prod"],
            "site-check stage argument comes from .Values.stage",
        ),
        (
            [
                "--set-json",
                'workloads.site-check.network.egress=[{"peer":"stage-site","fqdn":"other.example","port":443}]',
            ],
            "site-check egress comes from .Values.stage",
        ),
        (
            [*PROD, "--set-string", "edge.serveRoutes=yes"],
            "edge.serveRoutes must be true or false",
        ),
        (
            ["--set-string", "edge.serveRoutes=yes"],
            "edge.serveRoutes must be true or false",
        ),
    ],
)
def test_stage_guard(extra, error):
    result = render(*extra)
    assert result.returncode != 0
    assert error in result.stderr
