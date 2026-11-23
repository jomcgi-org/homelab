"""Validate the gVisor sandbox-fork slice 1: the SandboxConfig pins the fork
build, and the staging Job and its script check what they fetch. The slice is
wired with one idle worker; the wired resources' own test
(../resources/resources_test.py) lists the objects it applies and checks the
off switches (the suspended Job, no ForkSeed)."""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest
import yaml

_HERE = Path(__file__).resolve().parent
_ASSETS = json.loads((_HERE / "assets" / "assets.json").read_text())
_STAGE = _HERE / "assets" / "stage.py"
_GVISOR_DEFAULT_ANNOTATION = "sandboxconfig.ate.dev/is-class-default"
_SHA256 = "[0-9a-f]{64}"
_FORK_COMMIT = "0250f9acf"


def _kustomize(path: Path) -> list[dict]:
    out = subprocess.run(
        [os.environ["KUSTOMIZE_BIN"], "build", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _docs() -> list[dict]:
    return _kustomize(_HERE)


def _one(docs: list[dict], kind: str, name: str) -> dict:
    (found,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return found


def _job(docs: list[dict]) -> dict:
    return _one(docs, "Job", "gvisor-fork-assets-init")


def _containers(job: dict) -> list[dict]:
    pod = job["spec"]["template"]["spec"]
    return pod["initContainers"] + pod["containers"]


def test_sandboxconfig_pins_the_fork_build_and_is_not_the_class_default() -> None:
    config = _one(_docs(), "SandboxConfig", "gvisor-fork")
    assert config["spec"]["sandboxClass"] == "gvisor"
    # gvisor-default keeps the annotation: existing pools still resolve it.
    assert _GVISOR_DEFAULT_ANNOTATION not in config["metadata"].get("annotations", {})
    assert config["spec"]["defaultVersion"] == "v1"
    (version,) = config["spec"]["versions"]
    assert version["name"] == "v1"
    assert version["state"] == "Enabled"
    assert list(version["assets"]) == ["amd64"]
    # The admission policy wants exactly the "gvisor" release tarball asset.
    assert list(version["assets"]["amd64"]) == ["gvisor"]
    asset = version["assets"]["amd64"]["gvisor"]
    pinned = _ASSETS["assets"]["gvisor"]
    assert asset["sha256"] == pinned["sha256"]
    assert asset["url"] == (
        f"s3://{_ASSETS['bucket']}/{_ASSETS['prefix']}/{_ASSETS['arch']}/"
        f"{pinned['sha256']}{pinned['suffix']}"
    )
    # atelet reads the archive format from the suffix, and refuses to serve
    # the snapshot prefix as an asset.
    assert asset["url"].endswith(".tar")
    assert "/atespaces/" not in asset["url"]


def test_assets_pin_the_fork_build_and_its_helpers() -> None:
    source = _ASSETS["source"]
    assert source["url"].startswith(
        "https://github.com/jomcgi-org/gvisor/releases/download/"
    )
    assert _FORK_COMMIT in source["url"]
    assert source["strip"] == "release/"
    members = _ASSETS["members"]
    # runsc and the fork's forkd helper sit beside the other helpers.
    assert {"runsc", "gvisor-bin/forkd", "gvisor-bin/gvisor_sentry"} <= set(members)
    for sha in [
        source["sha256"],
        *members.values(),
        _ASSETS["assets"]["gvisor"]["sha256"],
    ]:
        assert len(sha) == 64 and set(sha) <= set("0123456789abcdef")


def test_asset_job_is_a_wave_1_sync_hook_with_pinned_images() -> None:
    job = _job(_docs())
    annotations = job["metadata"]["annotations"]
    assert annotations["argocd.argoproj.io/hook"] == "Sync"
    assert annotations["argocd.argoproj.io/sync-wave"] == "1"
    assert annotations["argocd.argoproj.io/hook-delete-policy"] == "BeforeHookCreation"
    for container in _containers(job):
        assert "@sha256:" in container["image"], container["name"]
        assert not container["image"].endswith(":latest")


def test_asset_job_is_bounded_and_locked_down() -> None:
    pod = _job(_docs())["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    for container in pod["initContainers"] + pod["containers"]:
        name = container["name"]
        limits = container["resources"]["limits"]
        assert "memory" in limits and "ephemeral-storage" in limits, name
        assert container["resources"]["requests"]["ephemeral-storage"], name
        context = container["securityContext"]
        assert context["allowPrivilegeEscalation"] is False, name
        assert context["capabilities"] == {"drop": ["ALL"]}, name
    (work,) = [v for v in pod["volumes"] if v["name"] == "work"]
    assert "sizeLimit" in work["emptyDir"]


def test_asset_job_mounts_its_generated_config_and_matches_the_pins() -> None:
    docs = _docs()
    job = _job(docs)
    (volume,) = [
        v for v in job["spec"]["template"]["spec"]["volumes"] if v["name"] == "config"
    ]
    config = _one(docs, "ConfigMap", volume["configMap"]["name"])
    assert set(config["data"]) == {"assets.json", "stage.py"}
    assert json.loads(config["data"]["assets.json"]) == _ASSETS
    assert config["data"]["stage.py"] == _STAGE.read_text()
    for container in _containers(job):
        env = {e["name"]: e["value"] for e in container.get("env", []) if "value" in e}
        if "BUCKET" in env:
            assert env["BUCKET"] == _ASSETS["bucket"]
            assert env["PREFIX"] == _ASSETS["prefix"]
            assert env["ARCH"] == _ASSETS["arch"]


def test_asset_job_reaches_only_rustfs_and_github_releases() -> None:
    docs = _docs()
    policy = _one(docs, "CiliumNetworkPolicy", "gvisor-fork-assets-init")["spec"]
    assert policy["endpointSelector"]["matchLabels"] == {
        "app": "gvisor-fork-assets-init"
    }
    assert "ingress" not in policy
    rustfs, github = policy["egress"]
    assert rustfs["toEndpoints"] == [{"matchLabels": {"app": "rustfs"}}]
    assert rustfs["toPorts"] == [{"ports": [{"port": "9000", "protocol": "TCP"}]}]
    assert {f["matchName"] for f in github["toFQDNs"]} == {
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    }
    assert github["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
    pod_labels = _job(docs)["spec"]["template"]["metadata"]["labels"]
    assert pod_labels == {"app": "gvisor-fork-assets-init"}
    # A separate policy admits the Job to rustfs; the wired one is untouched.
    admit = _one(docs, "CiliumNetworkPolicy", "rustfs-gvisor-fork-assets")["spec"]
    assert admit["endpointSelector"]["matchLabels"] == {"app": "rustfs"}
    (rule,) = admit["ingress"]
    assert rule["fromEndpoints"] == [
        {"matchLabels": {"app": "gvisor-fork-assets-init"}}
    ]


# stage.py against a synthetic build archive.

_FILES = {
    "runsc": b"runsc-bytes",
    "gvisor-bin/forkd": b"forkd-bytes",
    "gvisor-bin/gvisor_sentry": b"sentry-bytes",
}


def _release(path: Path, files: dict[str, bytes], extra=()) -> None:
    with tarfile.open(path, "w:gz") as out:
        root = tarfile.TarInfo("release")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        out.addfile(root)
        sub = tarfile.TarInfo("release/gvisor-bin")
        sub.type = tarfile.DIRTYPE
        sub.mode = 0o755
        out.addfile(sub)
        for name, data in {**files, **dict(extra)}.items():
            info = tarfile.TarInfo(f"release/{name}")
            info.mode = 0o555
            info.size = len(data)
            info.mtime = 1234567
            info.uid = 1001
            out.addfile(info, io.BytesIO(data))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stage(tmp_path: Path, files=None, extra=(), pin_wrong=False, existing=()):
    files = files or _FILES
    archive = tmp_path / "build.tar.gz"
    _release(archive, files, extra)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "existing.txt").write_text("".join(f"{k}\n" for k in existing))
    cfg = {
        "prefix": "p",
        "arch": "amd64",
        "source": {
            "url": archive.as_uri(),
            "sha256": _sha(archive.read_bytes()),
            "strip": "release/",
        },
        "assets": {"gvisor": {"sha256": "0" * 64, "suffix": ".tar"}},
        "members": {n: _sha(d) for n, d in _FILES.items()},
    }
    config = tmp_path / "assets.json"

    def run() -> subprocess.CompletedProcess:
        config.write_text(json.dumps(cfg))
        return subprocess.run(
            [sys.executable, "-I", str(_STAGE)],
            env={"ASSETS_CONFIG": str(config), "WORK_DIR": str(work)},
            capture_output=True,
            text=True,
        )

    # First run finds the repacked sha256; the pin is then set to it.
    first = run()
    assert first.returncode != 0 and "repacked tar: sha256" in first.stderr
    repacked = first.stderr.split("repacked tar: sha256 ")[1].split(",")[0]
    cfg["assets"]["gvisor"]["sha256"] = "1" * 64 if pin_wrong else repacked
    return run, work, repacked, cfg, config


def test_stage_script_repacks_the_build_deterministically(tmp_path: Path) -> None:
    run, work, repacked, _, _ = _stage(tmp_path)
    done = run()
    assert done.returncode == 0, done.stderr
    (out,) = (work / "assets").glob("*.tar")
    assert out.name == f"{repacked}.tar"
    assert _sha(out.read_bytes()) == repacked
    assert (work / "expected.txt").read_text() == f"p/amd64/{repacked}.tar\n"
    with tarfile.open(out) as tar:
        # No release/ prefix, runsc at the top, helpers under gvisor-bin/.
        names = [m.name for m in tar.getmembers()]
        assert names == sorted(names)
        assert names == [
            "gvisor-bin",
            "gvisor-bin/forkd",
            "gvisor-bin/gvisor_sentry",
            "runsc",
        ]
        for member in tar.getmembers():
            assert (member.uid, member.gid, member.mtime) == (0, 0, 0)
            assert member.uname == member.gname == ""
        assert tar.extractfile("gvisor-bin/forkd").read() == b"forkd-bytes"
    # A second run elsewhere gives the same bytes.
    again = run()
    assert again.returncode == 0, again.stderr
    assert _sha(out.read_bytes()) == repacked


def test_stage_script_skips_a_key_that_is_already_in_the_bucket(
    tmp_path: Path,
) -> None:
    run, work, repacked, _, _ = _stage(tmp_path)
    (work / "existing.txt").write_text(f"p/amd64/{repacked}.tar\n")
    done = run()
    assert done.returncode == 0, done.stderr
    assert "already in the bucket" in done.stdout
    assert list((work / "assets").iterdir()) == []


def test_stage_script_refuses_a_repacked_tar_that_is_not_the_pin(
    tmp_path: Path,
) -> None:
    run, work, _, _, _ = _stage(tmp_path, pin_wrong=True)
    done = run()
    assert done.returncode != 0
    assert "pinned " + "1" * 64 in done.stderr
    assert list((work / "assets").iterdir()) == []


def test_stage_script_refuses_a_changed_member(tmp_path: Path) -> None:
    run, work, _, _, _ = _stage(tmp_path)
    changed = {**_FILES, "gvisor-bin/forkd": b"other"}
    archive = tmp_path / "build.tar.gz"
    _release(archive, changed)
    cfg = json.loads((tmp_path / "assets.json").read_text())
    cfg["source"]["sha256"] = _sha(archive.read_bytes())
    (tmp_path / "assets.json").write_text(json.dumps(cfg))
    done = subprocess.run(
        [sys.executable, "-I", str(_STAGE)],
        env={"ASSETS_CONFIG": str(tmp_path / "assets.json"), "WORK_DIR": str(work)},
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "gvisor-bin/forkd: sha256" in done.stderr


@pytest.mark.parametrize(
    "files, extra, message",
    [
        (None, {"gvisor-bin/extra": b"x"}, "member not in the pinned list"),
        (
            {"runsc": _FILES["runsc"], "gvisor-bin/forkd": _FILES["gvisor-bin/forkd"]},
            {},
            "pinned members missing",
        ),
    ],
)
def test_stage_script_refuses_an_extra_or_missing_member(
    tmp_path: Path, files, extra, message: str
) -> None:
    archive = tmp_path / "build.tar.gz"
    _release(archive, files or _FILES, extra)
    work = tmp_path / "work"
    work.mkdir()
    (work / "existing.txt").write_text("")
    config = tmp_path / "assets.json"
    config.write_text(
        json.dumps(
            {
                "prefix": "p",
                "arch": "amd64",
                "source": {
                    "url": archive.as_uri(),
                    "sha256": _sha(archive.read_bytes()),
                    "strip": "release/",
                },
                "assets": {"gvisor": {"sha256": "0" * 64, "suffix": ".tar"}},
                "members": {n: _sha(d) for n, d in _FILES.items()},
            }
        )
    )
    done = subprocess.run(
        [sys.executable, "-I", str(_STAGE)],
        env={"ASSETS_CONFIG": str(config), "WORK_DIR": str(work)},
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert message in done.stderr


def test_stage_script_refuses_a_build_archive_with_the_wrong_hash(
    tmp_path: Path,
) -> None:
    run, work, _, cfg, config = _stage(tmp_path)
    cfg["source"]["sha256"] = "2" * 64
    config.write_text(json.dumps(cfg))
    done = subprocess.run(
        [sys.executable, "-I", str(_STAGE)],
        env={"ASSETS_CONFIG": str(config), "WORK_DIR": str(work)},
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "build archive: sha256" in done.stderr


# The kvm pool.

_NS = "substrate-gvisor-fork"
_FORK_IMAGES = "ghcr.io/jomcgi-org/homelab/substrate-fork/"


def _pool() -> dict:
    return _one(_docs(), "WorkerPool", "gvisor-fork")


def test_kvm_pool_has_one_worker_and_uses_the_fork_config() -> None:
    pool = _pool()
    assert pool["metadata"]["namespace"] == _NS
    # Step 6 of the enable steps: one worker, with ForkSeed still off. The pool
    # has no autoscaler, so the count is fixed.
    assert pool["spec"]["replicas"] == 1
    (klass,) = pool["spec"]["sandboxClasses"]
    assert klass == {
        "name": "gvisor",
        "platform": "kvm",
        "configRef": {"name": "gvisor-fork"},
    }
    # Dedicated: no other pool or SandboxConfig is named, and gvisor-default
    # is not this pool's config.
    assert klass["configRef"]["name"] != "gvisor-default"


def test_kvm_pool_template_sets_only_the_account_the_placement_and_the_resources() -> (
    None
):
    # No tolerations, hard node selector, volumes, host namespaces or security
    # context: the controller shapes the pod, and a new field is a review item.
    template = _pool()["spec"]["template"]
    assert set(template) == {"serviceAccountName", "nodeAffinity", "resources"}
    # Node-1 is preferred, not required: a drained or tainted node-1 must not
    # leave the single worker pending.
    assert set(template["nodeAffinity"]) == {
        "preferredDuringSchedulingIgnoredDuringExecution"
    }
    (term,) = template["nodeAffinity"][
        "preferredDuringSchedulingIgnoredDuringExecution"
    ]
    assert term["weight"] == 100
    assert term["preference"] == {
        "matchExpressions": [
            {"key": "kubernetes.io/hostname", "operator": "In", "values": ["node-1"]}
        ]
    }


def test_kvm_pool_with_workers_has_a_real_worker_image_digest() -> None:
    # An all-zero digest names no image: a pool that starts workers must not
    # carry one.
    pool = _pool()
    if pool["spec"]["replicas"] > 0:
        digest = pool["spec"]["workerImage"].split("@sha256:")[1]
        assert set(digest) != {"0"}
        assert len(digest) == 64


def test_kvm_pool_runs_the_fork_worker_image_by_digest() -> None:
    image = _pool()["spec"]["workerImage"]
    assert image.startswith(_FORK_IMAGES + "ateworker-gvisor:")
    # Never one of the production names the cluster runs.
    assert "ghcr.io/kagent-dev/" not in image
    assert "@sha256:" in image


def test_kvm_pool_requests_1gi_and_limits_for_a_seed_and_8_children() -> None:
    resources = _pool()["spec"]["template"]["resources"]
    # The egress-scan design (egress-scan/ARCHITECTURE.md, "Capacity"): a
    # scheduling reservation of 1Gi and 500m. On 2026-11-23 node-1 had 5493Mi
    # and 8285m left to request, node-3 2601Mi and node-2 1981Mi.
    assert resources["requests"] == {"cpu": "500m", "memory": "1Gi"}
    # The seed (1000m, 512 MiB cap) and 8 children (1000m, 128 MiB + 16 MiB of
    # overhead each): 9000m and 512 + 8 x 144 = 1664 MiB booked.
    assert resources["limits"] == {"cpu": "9", "memory": "2Gi"}
    assert 512 + 8 * (128 + 16) <= 2 * 1024
    assert "ate.dev/kvm" not in resources["limits"]  # the controller adds it
    assert _pool()["spec"]["template"]["serviceAccountName"] == "fork-worker"


def test_kvm_pool_namespace_allows_only_the_router_and_the_gateway() -> None:
    docs = _docs()
    namespace = _one(docs, "Namespace", _NS)
    assert namespace["metadata"]["labels"] == {
        "factory.jomcgi.dev/substrate-workers": "true"
    }
    account = _one(docs, "ServiceAccount", "fork-worker")
    assert account["automountServiceAccountToken"] is False
    policy = _one(docs, "CiliumNetworkPolicy", "substrate-workers")["spec"]
    assert policy["endpointSelector"]["matchExpressions"] == [
        {"key": "ate.dev/worker-pool", "operator": "Exists"}
    ]
    (ingress,) = policy["ingress"]
    assert ingress["fromEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "ate-system",
                "app": "atenet-router",
            }
        }
    ]
    (egress,) = policy["egress"]
    assert egress["toEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "ate-system",
                "app": "atenet-egress",
            }
        }
    ]
    for rule in (ingress, egress):
        assert rule["toPorts"] == [{"ports": [{"port": "8443", "protocol": "TCP"}]}]


# The conformance Job and its script.

_CONFORMANCE = _HERE / "conformance" / "conformance.py"
_TEMPLATE = _HERE / "conformance" / "template.yaml"


def _conformance_job() -> dict:
    return _one(_docs(), "Job", "gvisor-fork-conformance-3")


def test_conformance_job_is_suspended_and_runs_as_ate_client() -> None:
    job = _conformance_job()
    assert job["spec"]["suspend"] is True
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] <= 3600
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "ate-client"
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    # The only credential is the audience-bound token for the ate API.
    (token,) = [v for v in pod["volumes"] if v["name"] == "ate-token"]
    (source,) = token["projected"]["sources"]
    assert source["serviceAccountToken"]["audience"] == "api.ate-system.svc"
    (container,) = pod["containers"]
    assert "@sha256:" in container["image"]
    assert not [e for e in container["env"] if "valueFrom" in e]
    context = container["securityContext"]
    assert context["allowPrivilegeEscalation"] is False
    assert context["readOnlyRootFilesystem"] is True
    assert context["capabilities"] == {"drop": ["ALL"]}
    assert {"memory", "ephemeral-storage"} <= set(container["resources"]["limits"])


def test_conformance_job_has_time_to_clean_up_on_sigterm() -> None:
    pod = _conformance_job()["spec"]["template"]["spec"]
    # Cleanup waits up to STATE_WAIT_SECONDS (120) for the seed to go.
    assert pod["terminationGracePeriodSeconds"] >= 240


def test_conformance_job_takes_the_cli_from_the_fork_image_by_digest() -> None:
    pod = _conformance_job()["spec"]["template"]["spec"]
    (cli,) = [v for v in pod["volumes"] if v["name"] == "cli"]
    reference = cli["image"]["reference"]
    assert reference.startswith(_FORK_IMAGES + "kubectl-ate:")
    assert "@sha256:" in reference
    (container,) = pod["containers"]
    (mount,) = [m for m in container["volumeMounts"] if m["name"] == "cli"]
    assert mount["readOnly"] is True
    assert pod["imagePullSecrets"] == [{"name": "ghcr-imagepull-secret"}]


def test_conformance_job_reaches_only_the_ate_api_and_the_kube_api() -> None:
    docs = _docs()
    policy = _one(docs, "CiliumNetworkPolicy", "gvisor-fork-conformance")["spec"]
    assert policy["endpointSelector"]["matchLabels"] == {
        "app": "gvisor-fork-conformance"
    }
    assert "ingress" not in policy
    kube, api = policy["egress"]
    assert kube["toEntities"] == ["kube-apiserver"]
    assert kube["toPorts"] == [{"ports": [{"port": "6443", "protocol": "TCP"}]}]
    assert api["toEndpoints"] == [{"matchLabels": {"app": "ate-api-server"}}]
    assert api["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
    admit = _one(docs, "CiliumNetworkPolicy", "ate-api-server-gvisor-fork-conformance")
    (rule,) = admit["spec"]["ingress"]
    assert rule["fromEndpoints"] == [
        {"matchLabels": {"app": "gvisor-fork-conformance"}}
    ]
    labels = _conformance_job()["spec"]["template"]["metadata"]["labels"]
    assert labels == {"app": "gvisor-fork-conformance"}


def test_conformance_job_grants_nothing_new() -> None:
    kinds = {d["kind"] for d in _docs()}
    assert not kinds & {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}


def test_conformance_job_mounts_its_generated_config() -> None:
    docs = _docs()
    pod = _conformance_job()["spec"]["template"]["spec"]
    (volume,) = [v for v in pod["volumes"] if v["name"] == "config"]
    config = _one(docs, "ConfigMap", volume["configMap"]["name"])
    assert config["data"] == {
        "conformance.py": _CONFORMANCE.read_text(),
        "template.yaml": _TEMPLATE.read_text(),
    }


def test_conformance_template_is_a_fork_template_with_no_egress_and_no_env() -> None:
    template = yaml.safe_load(_TEMPLATE.read_text())
    fork = template["forkConfig"]
    assert fork["mode"] == "FORK_MODE_SEED_AND_FORK"
    assert fork["singleUse"] is True
    assert fork["egress"] == "FORK_EGRESS_NONE"
    assert 1 <= fork["maxLifetimeSeconds"] <= 3600
    (container,) = template["containers"]
    assert "env" not in container
    assert "@sha256:" in container["image"]
    assert container["wakeupProbe"]["httpGet"]["path"] == "/healthz"
    assert template["workerSelector"] == {"matchLabels": {"workload": "gvisor-fork"}}
    # The pool the template selects is the one kvm-pool.yaml defines.
    assert _pool()["metadata"]["labels"] == {"workload": "gvisor-fork"}
    # A forked actor's private limit fits the seed's.
    private = template["resources"]["limits"]["memoryBytes"]
    assert private < fork["seedResources"]["limits"]["memoryBytes"]


# conformance.py against a fake kubectl-ate that answers in the API's JSON shapes:
# Actor {metadata, actorTemplate, status: {state, fork: {role}}} and
# WorkerSeed {seedActor: {atespace, name}, seedActorUid, templateUid, workerEpoch}.

_FAKE = """\
import json, os, re, sys
S = os.environ["FAKE_STATE"]
st = json.load(open(S)) if os.path.exists(S) else {
    "atespaces": [], "templates": {}, "actors": {}, "listed": {}, "calls": [],
    "next": 0}
a = sys.argv[1:]
preview = False
while a and a[0] in ("--endpoint", "--token-file", "--preview"):
    if a[0] == "--preview":
        preview, a = True, a[1:]
    else:
        a = a[2:]
st["calls"].append(("preview " if preview else "") + " ".join(a))
def save(): json.dump(st, open(S, "w"))
def flag(name):
    return a[a.index(name) + 1] if name in a else None
def fail(msg):
    save(); print(msg, file=sys.stderr); sys.exit(1)
def out(obj):
    save(); print(json.dumps(obj)); sys.exit(0)
env = os.environ.get
def seed_rows():
    if env("FAKE_FOREIGN_SEED"):
        return [{"seedActor": {"atespace": "elsewhere", "name": "seed-x"},
                 "seedActorUid": "u-seed", "templateUid": "u-other", "workerEpoch": 1}]
    return [{"seedActor": {"atespace": t["ns"], "name": "seed-" + t["name"]},
             "seedActorUid": "u-seed-" + t["name"], "templateUid": t["uid"],
             "workerEpoch": 1} for t in st["templates"].values()]
def template_json(t):
    return {"metadata": {"atespace": t["ns"], "name": t["name"], "uid": t["uid"]},
            "containers": [{"name": "engine"}]}
verb = a[:2]
if verb == ["get", "workers"]:
    state = env("FAKE_WORKER_STATE", "WORKER_STATE_ACTIVE")
    out({"workers": [
        {"workerPool": "other", "status": {"state": "WORKER_STATE_ACTIVE"}},
        {"workerPool": "gvisor-fork", "status": {"state": state, "seeds": seed_rows()}}]})
if verb == ["create", "atespace"]:
    st["atespaces"].append(a[2]); out({})
if verb == ["create", "actor-template"]:
    text = open(a[3]).read()
    bad = ("API_TOKEN" in text or "singleUse: false" in text
           or "FORK_EGRESS_GATEWAY" in text)
    if bad and env("FAKE_INFRA_REFUSAL"):
        fail("rpc error: code = Unavailable desc = connection refused")
    if bad and not env("FAKE_ACCEPT_BAD"):
        fail("rpc error: code = InvalidArgument desc = invalid fork template")
    ns = re.search(r"(?m)^  atespace: (.*)$", text).group(1)
    name = re.search(r"(?m)^  name: (.*)$", text).group(1)
    st["next"] += 1
    st["templates"][ns + "/" + name] = {"ns": ns, "name": name, "uid": "u-t%d" % st["next"]}
    out({})
if verb == ["get", "actor-template"]:
    ns = flag("-a")
    rows = [template_json(t) for t in st["templates"].values() if t["ns"] == ns]
    if len(a) > 2 and not a[2].startswith("-"):
        rows = [r for r in rows if r["metadata"]["name"] == a[2]]
        if not rows:
            fail("rpc error: code = NotFound")
        out(rows[0])
    out({"actorTemplates": rows})
if verb == ["create", "actor"]:
    ns, template = flag("-a"), flag("--template")
    if not preview:
        fail("rpc error: code = FailedPrecondition desc = ActorTemplate uses fork_config, "
             "which needs the ForkSeed preview gate to be enabled on the server and "
             "the client to opt in to preview features")
    if "/" in template and template.split("/")[0] != ns and env("FAKE_OTHER_REASON"):
        fail("rpc error: code = FailedPrecondition desc = some other refusal")
    if "/" in template and template.split("/")[0] != ns and not env("FAKE_ALLOW_CROSS"):
        fail("rpc error: code = PermissionDenied desc = ActorTemplate " + template +
             " uses fork_config, so only Actors in atespace '" + template.split("/")[0] +
             "' may use it")
    st["actors"][ns + "/" + a[2]] = {"name": a[2], "ns": ns, "state": "ACTOR_STATE_SUSPENDED"}
    out({})
if verb == ["resume", "actor"]:
    actor = st["actors"][flag("-a") + "/" + a[2]]
    if actor["state"] == "ACTOR_STATE_RUNNING":
        fail("rpc error: code = FailedPrecondition desc = a forked actor runs once")
    actor["state"] = "ACTOR_STATE_RUNNING"; out({})
if verb == ["suspend", "actor"]:
    if not env("FAKE_ALLOW_SUSPEND"):
        fail("rpc error: code = FailedPrecondition desc = a forked actor cannot suspend")
    out({})
if verb == ["get", "actors"]:
    ns = flag("-a")
    if ns not in st["atespaces"]:
        fail("rpc error: code = NotFound desc = atespace")
    rows = []
    for key, actor in list(st["actors"].items()):
        if actor["ns"] != ns:
            continue
        st["listed"][key] = st["listed"].get(key, 0) + 1
        if st["listed"][key] > 4 and not env("FAKE_NO_EXPIRE"):
            del st["actors"][key]  # the lifetime passed
            continue
        rows.append({"metadata": {"atespace": ns, "name": actor["name"]},
                     "actorTemplate": {"atespace": ns, "name": "engine"},
                     "status": {"state": actor["state"],
                                "fork": {"role": "FORK_ROLE_FORK"}}})
    for t in st["templates"].values():
        if t["ns"] == ns:
            rows.append({"metadata": {"atespace": ns, "name": "seed-" + t["name"]},
                         "status": {"state": "ACTOR_STATE_RUNNING",
                                    "fork": {"role": "FORK_ROLE_SEED"}}})
    out({"actors": rows})
if verb == ["delete", "actor"]:
    st["actors"].pop(flag("-a") + "/" + a[2], None); out({})
if verb == ["delete", "actor-template"]:
    st["templates"].pop(flag("-a") + "/" + a[2], None); out({})
if verb == ["delete", "atespace"]:
    ns = a[2]
    left = [k for k in list(st["actors"]) + list(st["templates"]) if k.startswith(ns + "/")]
    if left:
        fail("rpc error: code = FailedPrecondition desc = atespace is not empty: " + left[0])
    st["atespaces"].remove(ns); out({})
fail("fake: unknown " + " ".join(a))
"""


def _conformance(tmp_path: Path, template=None, **env):
    fake = tmp_path / "kubectl-ate"
    fake.write_text(f"#!{sys.executable}\n" + _FAKE)
    fake.chmod(0o755)
    text = _TEMPLATE.read_text() if template is None else template
    path = tmp_path / "template.yaml"
    path.write_text(text)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    state = tmp_path / "state.json"
    run = subprocess.run(
        [sys.executable, "-I", str(_CONFORMANCE)],
        env={
            "ATE_BIN": str(fake),
            "FAKE_STATE": str(state),
            "TEMPLATE_FILE": str(path),
            "WORK_DIR": str(work),
            "POLL_SECONDS": "0.01",
            "WORKER_WAIT_SECONDS": "0.5",
            "SEED_WAIT_SECONDS": "0.5",
            "STATE_WAIT_SECONDS": "1",
            "LIFETIME_WAIT_SECONDS": "2",
            "FORKS": "3",
            **env,
        },
        capture_output=True,
        text=True,
    )
    final = json.loads(state.read_text()) if state.exists() else None
    return run, final


def _engine_template() -> str:
    return _TEMPLATE.read_text()


def _placeholder_template() -> str:
    # The all-zero digest that stood in before the engine image existed.
    return re.sub(r"@sha256:[0-9a-f]{64}", "@sha256:" + "0" * 64, _TEMPLATE.read_text())


def _nothing_left(state: dict) -> bool:
    return not (state["atespaces"] or state["templates"] or state["actors"])


def test_conformance_passes_all_eight_checks(tmp_path: Path) -> None:
    run, state = _conformance(tmp_path, _engine_template())
    assert run.returncode == 0, run.stdout + run.stderr
    for check in ("C1", "C3", "C4", "C5", "C7", "C8"):
        assert f"check={check} result=pass" in run.stdout, check
    assert run.stdout.count("check=C2 result=pass") == 3
    assert run.stdout.count("check=C6 result=pass") == 2
    # The log carries the API's reason for each refusal.
    assert "InvalidArgument" in run.stdout and "PermissionDenied" in run.stdout
    assert "event=summary result=ok" in run.stdout
    assert (
        "preview create actor fork-3 -a fork-conformance --template engine"
        in state["calls"]
    )
    # Every call carries --preview: the API refuses fork actors without it.
    assert all(c.startswith("preview ") for c in state["calls"])
    # Both atespaces went, so no actor or template was left to refuse it.
    assert _nothing_left(state)


def test_conformance_matches_the_seed_by_atespace_and_template_uid(
    tmp_path: Path,
) -> None:
    # The worker lists a seed, but of another atespace and template.
    run, state = _conformance(tmp_path, _engine_template(), FAKE_FOREIGN_SEED="1")
    assert run.returncode == 1
    assert "a ready seed: not ready" in run.stdout
    assert _nothing_left(state)


def test_conformance_refuses_a_placeholder_engine_image_before_any_api_call(
    tmp_path: Path,
) -> None:
    run, state = _conformance(tmp_path, _placeholder_template())
    assert run.returncode == 1
    assert "no real sha256 digest" in run.stdout
    assert state is None  # the fake CLI never ran


@pytest.mark.parametrize(
    "image",
    ["python:3.14", "x@sha256:abc", "x@sha256:" + "G" * 64],
)
def test_conformance_refuses_an_unpinned_engine_image(
    tmp_path: Path, image: str
) -> None:
    text = re.sub(r"(?m)^(    image: ).*$", rf"\g<1>{image}", _TEMPLATE.read_text())
    run, state = _conformance(tmp_path, text)
    assert run.returncode == 1
    assert state is None


@pytest.mark.parametrize(
    "env, message",
    [
        ({"FAKE_ACCEPT_BAD": "1"}, "C2: the API accepted a template with secret_env"),
        ({"FAKE_INFRA_REFUSAL": "1"}, "failed, but not as a refusal"),
        ({"FAKE_ALLOW_CROSS": "1"}, "C5: the API accepted an actor of another"),
        (
            {"FAKE_OTHER_REASON": "1"},
            "C5: an actor of another atespace using the template was refused for another reason",
        ),
        ({"FAKE_ALLOW_SUSPEND": "1"}, "C6: the API accepted a suspend"),
        ({"FAKE_NO_EXPIRE": "1"}, "forks deleted by their lifetime: not ready"),
        ({"FAKE_WORKER_STATE": "WORKER_STATE_UNAVAILABLE"}, "an ACTIVE gvisor-fork"),
    ],
)
def test_conformance_fails_on_each_broken_guarantee_and_leaves_nothing(
    tmp_path: Path, env: dict, message: str
) -> None:
    run, state = _conformance(tmp_path, _engine_template(), **env)
    assert run.returncode == 1
    assert message in run.stdout
    assert "event=summary result=fail" in run.stdout
    # Cleanup deleted the forks, the templates (the engine and any accepted
    # bad one) and then the atespaces, which the API refuses while they hold
    # a child.
    assert _nothing_left(state)


def test_conformance_refuses_a_template_it_cannot_edit(tmp_path: Path) -> None:
    run, state = _conformance(
        tmp_path, _engine_template().replace("singleUse: true", "singleUse: yes")
    )
    assert run.returncode == 1
    assert "has no 'singleUse: true' to edit" in run.stdout
    assert _nothing_left(state)


def test_staged_images_name_the_published_fork_digests() -> None:
    # The references the substrate-fork-images and substrate-fork-engine
    # workflows published (their run summaries). The test compares strings: it
    # does not pull the images.
    template = yaml.safe_load(_TEMPLATE.read_text())
    (container,) = template["containers"]
    pod = _conformance_job()["spec"]["template"]["spec"]
    (cli,) = [v for v in pod["volumes"] if v["name"] == "cli"]
    images = {
        "engine": container["image"],
        "cli": cli["image"]["reference"],
        "worker": _pool()["spec"]["workerImage"],
    }
    assert images == {
        "engine": _FORK_IMAGES
        + "engine-oss:1.186.0-946d07e5597c@sha256:"
        + "dbffb4d73f072b2a71a05356a657a595bd147357bc85a538139f2634be47b238",
        "cli": _FORK_IMAGES
        + "kubectl-ate:fork-1e7a877d0ca3@sha256:"
        + "ce43eb79787738134a3de680fb660da96eb4a9ad13e48a088bc899d0a654d4ed",
        "worker": _FORK_IMAGES
        + "ateworker-gvisor:fork-1e7a877d0ca3@sha256:"
        + "f208bf5ae975a2360bfb42a8d38b2eb3fdaba537a2078332af5dcd545a17a7d1",
    }


# The latency Jobs and their script.

_LATENCY = _HERE / "latency" / "latency.py"
_LATENCY_TEMPLATE = _HERE / "latency" / "template.yaml"
_LATENCY_JOBS = {
    "gvisor-fork-latency-1": ("gvisor-fork-latency", "control"),
    "gvisor-fork-latency-data-1": ("gvisor-fork-latency-data", "all"),
}


def _latency_docs() -> list[dict]:
    return _kustomize(_HERE / "latency")


def _latency_job(name: str) -> dict:
    return _one(_latency_docs(), "Job", name)


def _env(container: dict) -> dict:
    return {e["name"]: e.get("value") for e in container["env"]}


def test_latency_directory_is_staged_and_not_listed_by_the_parent() -> None:
    parent = yaml.safe_load((_HERE / "kustomization.yaml").read_text())
    assert not [r for r in parent["resources"] if "latency" in r]
    assert "gvisor-fork-latency" not in {
        d["metadata"]["name"] for d in _docs() if d["kind"] in ("Job", "ConfigMap")
    }
    # Both Jobs are suspended: wired, they would hold the Application in health
    # Suspended, so the change that runs them lists the directory.
    for name in _LATENCY_JOBS:
        assert _latency_job(name)["spec"]["suspend"] is True


@pytest.mark.parametrize("name", list(_LATENCY_JOBS))
def test_latency_jobs_are_suspended_and_run_as_ate_client(name: str) -> None:
    label, mode = _LATENCY_JOBS[name]
    job = _latency_job(name)
    assert job["spec"]["suspend"] is True
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] <= 3600
    pod = job["spec"]["template"]["spec"]
    assert job["spec"]["template"]["metadata"]["labels"] == {"app": label}
    assert pod["serviceAccountName"] == "ate-client"
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    # SIGTERM cleanup waits up to STATE_WAIT_SECONDS (120) for the seed to go.
    assert pod["terminationGracePeriodSeconds"] >= 240
    (container,) = pod["containers"]
    assert "@sha256:" in container["image"]
    assert not [e for e in container["env"] if "valueFrom" in e]
    assert _env(container)["MODE"] == mode
    context = container["securityContext"]
    assert context["allowPrivilegeEscalation"] is False
    assert context["readOnlyRootFilesystem"] is True
    assert context["capabilities"] == {"drop": ["ALL"]}
    assert {"memory", "ephemeral-storage"} <= set(container["resources"]["limits"])
    # The only API credential is the audience-bound token, as in conformance.
    (token,) = [v for v in pod["volumes"] if v["name"] == "ate-token"]
    (source,) = token["projected"]["sources"]
    assert source["serviceAccountToken"]["audience"] == "api.ate-system.svc"


def test_latency_jobs_grant_nothing_new() -> None:
    kinds = {d["kind"] for d in _latency_docs()}
    assert not kinds & {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}


def test_latency_jobs_use_the_conformance_cli_by_digest() -> None:
    reference = {}
    for name in [*_LATENCY_JOBS, "gvisor-fork-conformance-3"]:
        pod = (_latency_job(name) if name in _LATENCY_JOBS else _conformance_job())[
            "spec"
        ]["template"]["spec"]
        (cli,) = [v for v in pod["volumes"] if v["name"] == "cli"]
        reference[name] = cli["image"]["reference"]
        assert pod["imagePullSecrets"] == [{"name": "ghcr-imagepull-secret"}]
    assert len(set(reference.values())) == 1


def test_only_the_data_job_holds_a_router_identity() -> None:
    control = _latency_job("gvisor-fork-latency-1")["spec"]["template"]["spec"]
    assert {v["name"] for v in control["volumes"]} == {
        "tmp",
        "config",
        "cli",
        "ate-token",
    }
    data = _latency_job("gvisor-fork-latency-data-1")["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in data["volumes"]}
    (cert,) = volumes["router"]["projected"]["sources"]
    # The client certificate the router checks, from the signer the router's
    # own manifest names for pod identity.
    assert cert["podCertificate"]["signerName"] == (
        "podidentity.podcert.ate.dev/identity"
    )
    (ca,) = volumes["router-ca"]["projected"]["sources"]
    assert ca["clusterTrustBundle"]["signerName"] == (
        "servicedns.podcert.ate.dev/identity"
    )
    (container,) = data["containers"]
    paths = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    # The script's default paths are the mounts the Job makes.
    latency = _load_latency()
    assert latency.ROUTER_CERT == paths["router"] + "/credential-bundle.pem"
    assert latency.ROUTER_CA == paths["router-ca"] + "/trust-bundle.pem"
    assert latency.ROUTER_URL == "https://atenet-router.ate-system.svc:443"
    assert paths["router"] == "/var/run/router"
    assert paths["router-ca"] == "/var/run/router-ca"


def test_latency_jobs_reach_only_the_api_and_the_data_job_the_router() -> None:
    docs = _latency_docs()
    control = _one(docs, "CiliumNetworkPolicy", "gvisor-fork-latency")["spec"]
    data = _one(docs, "CiliumNetworkPolicy", "gvisor-fork-latency-data")["spec"]
    assert control["endpointSelector"]["matchLabels"] == {"app": "gvisor-fork-latency"}
    assert data["endpointSelector"]["matchLabels"] == {
        "app": "gvisor-fork-latency-data"
    }
    assert "ingress" not in control and "ingress" not in data
    kube, api = control["egress"]
    assert kube["toEntities"] == ["kube-apiserver"]
    assert api["toEndpoints"] == [{"matchLabels": {"app": "ate-api-server"}}]
    assert api["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
    assert data["egress"][:2] == control["egress"]
    (router,) = data["egress"][2:]
    assert router["toEndpoints"] == [{"matchLabels": {"app": "atenet-router"}}]
    assert router["toPorts"] == [{"ports": [{"port": "8443", "protocol": "TCP"}]}]
    admit = _one(docs, "CiliumNetworkPolicy", "ate-api-server-gvisor-fork-latency")
    (rule,) = admit["spec"]["ingress"]
    assert rule["fromEndpoints"] == [
        {"matchLabels": {"app": "gvisor-fork-latency"}},
        {"matchLabels": {"app": "gvisor-fork-latency-data"}},
    ]


def test_latency_jobs_mount_their_generated_config() -> None:
    docs = _latency_docs()
    for name in _LATENCY_JOBS:
        pod = _latency_job(name)["spec"]["template"]["spec"]
        (volume,) = [v for v in pod["volumes"] if v["name"] == "config"]
        config = _one(docs, "ConfigMap", volume["configMap"]["name"])
        assert config["data"] == {
            "latency.py": _LATENCY.read_text(),
            "template.yaml": _LATENCY_TEMPLATE.read_text(),
        }


def test_latency_template_is_a_fork_template_with_no_egress_and_no_env() -> None:
    template = yaml.safe_load(_LATENCY_TEMPLATE.read_text())
    fork = template["forkConfig"]
    assert fork["mode"] == "FORK_MODE_SEED_AND_FORK"
    assert fork["singleUse"] is True
    assert fork["egress"] == "FORK_EGRESS_NONE"
    # A pool child waits for its request: the lifetime must cover the wait.
    assert 60 <= fork["maxLifetimeSeconds"] <= 3600
    (container,) = template["containers"]
    assert "env" not in container
    # The same public engine image the conformance Job forks.
    conformance = yaml.safe_load(_TEMPLATE.read_text())
    assert container["image"] == conformance["containers"][0]["image"]
    assert template["workerSelector"] == conformance["workerSelector"]
    # The router sends a request to port 80 unless the client names another.
    assert container["command"][-1] == "0.0.0.0:80"
    assert container["wakeupProbe"]["httpGet"] == {"path": "/healthz", "port": 80}
    assert template["metadata"]["atespace"] == "fork-latency"
    assert template["metadata"]["atespace"] != conformance["metadata"]["atespace"]
    assert (
        template["resources"]["limits"]["memoryBytes"]
        < (fork["seedResources"]["limits"]["memoryBytes"])
    )


def _load_latency():
    spec = importlib.util.spec_from_file_location("latency_under_test", _LATENCY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_percentiles_are_nearest_rank() -> None:
    latency = _load_latency()
    values = [float(i) for i in range(1, 101)]
    assert [latency.percentile(values, q) for q in (0.5, 0.95, 0.99)] == [50, 95, 99]
    # With fewer than 100 samples p99 is the maximum.
    twenty = [float(i) for i in range(1, 21)]
    assert latency.percentile(twenty, 0.99) == 20
    assert latency.percentile(twenty, 0.95) == 19
    assert latency.percentile([7.0], 0.5) == 7.0


def test_request_body_has_the_exact_size_and_one_fake_secret() -> None:
    latency = _load_latency()
    for size in (1024, 16384, 524288):
        body = latency.body_of(size)
        assert len(body) == size
        assert body.count(b"AKIAIOSFODNN7EXAMPLE") == 1
        body.decode("utf-8")


# latency.py against a fake kubectl-ate and a fake router. The fake CLI keeps
# its state in a file under a lock, because the script calls it from threads.

_LATENCY_FAKE = """\
import fcntl, json, os, re, sys
S = os.environ["FAKE_STATE"]
lock = open(S + ".lock", "w")
fcntl.flock(lock, fcntl.LOCK_EX)
st = json.load(open(S)) if os.path.exists(S) else {
    "atespaces": [], "templates": {}, "actors": {}, "calls": [], "next": 0}
a = sys.argv[1:]
preview = False
while a and a[0] in ("--endpoint", "--token-file", "--preview"):
    if a[0] == "--preview":
        preview, a = True, a[1:]
    else:
        a = a[2:]
st["calls"].append(("preview " if preview else "") + " ".join(a))
def save(): json.dump(st, open(S, "w"))
def flag(name):
    return a[a.index(name) + 1] if name in a else None
def fail(msg):
    save(); print(msg, file=sys.stderr); sys.exit(1)
def out(obj):
    save(); print(json.dumps(obj)); sys.exit(0)
consumed = set()
if os.path.exists(S + ".consumed"):
    consumed = set(open(S + ".consumed").read().split())
# A child exits after its one request and the API deletes its actor.
for gone in consumed:
    st["actors"].pop(gone, None)
verb = a[:2]
if verb == ["get", "workers"]:
    seeds = [{"seedActor": {"atespace": t["ns"], "name": "seed-" + t["name"]},
              "seedActorUid": "u-seed", "templateUid": t["uid"], "workerEpoch": 1}
             for t in st["templates"].values()]
    status = {"state": "WORKER_STATE_ACTIVE", "seeds": seeds}
    if os.environ.get("FAKE_CAP_CPU"):
        # protojson writes an int64 as a string.
        status["capacity"] = {"cpuMillicores": os.environ["FAKE_CAP_CPU"],
                              "memoryBytes": os.environ["FAKE_CAP_MEM"]}
        status["allocated"] = {"cpuMillicores": "1000", "memoryBytes": "268435456"}
    out({"workers": [{"workerPool": "gvisor-fork", "status": status}]})
if verb == ["create", "atespace"]:
    st["atespaces"].append(a[2]); out({})
if verb == ["create", "actor-template"]:
    text = open(a[3]).read()
    ns = re.search(r"(?m)^  atespace: (.*)$", text).group(1)
    name = re.search(r"(?m)^  name: (.*)$", text).group(1)
    st["next"] += 1
    st["templates"][ns + "/" + name] = {"ns": ns, "name": name, "uid": "u-t%d" % st["next"]}
    out({})
if verb == ["get", "actor-template"]:
    rows = [{"metadata": {"atespace": t["ns"], "name": t["name"], "uid": t["uid"]}}
            for t in st["templates"].values() if t["ns"] == flag("-a")]
    out(rows[0] if len(a) > 2 and not a[2].startswith("-") else {"actorTemplates": rows})
if verb == ["create", "actor"]:
    if not preview:
        fail("rpc error: code = FailedPrecondition desc = needs the ForkSeed preview gate")
    st["actors"][a[2]] = {"name": a[2], "state": "ACTOR_STATE_SUSPENDED"}
    st["peak"] = max(st.get("peak", 0), len(st["actors"]))
    out({})
if verb == ["resume", "actor"]:
    st["actors"][a[2]]["state"] = "ACTOR_STATE_RUNNING"; out({})
if verb == ["get", "actors"]:
    if flag("-a") not in st["atespaces"]:
        fail("rpc error: code = NotFound desc = atespace")
    rows = [{"metadata": {"atespace": flag("-a"), "name": x["name"]},
             "status": {"state": x["state"], "fork": {"role": "FORK_ROLE_FORK"}}}
            for x in st["actors"].values() if x["name"] not in consumed]
    for t in st["templates"].values():
        rows.append({"metadata": {"atespace": t["ns"], "name": "seed-" + t["name"]},
                     "status": {"state": "ACTOR_STATE_RUNNING",
                                "fork": {"role": "FORK_ROLE_SEED"}}})
    out({"actors": rows})
if verb == ["delete", "actor"]:
    st["actors"].pop(a[2], None); out({})
if verb == ["delete", "actor-template"]:
    st["templates"].pop(flag("-a") + "/" + a[2], None); out({})
if verb == ["delete", "atespace"]:
    if st["actors"]:
        fail("rpc error: code = FailedPrecondition desc = atespace is not empty")
    st["atespaces"].remove(a[2]); out({})
fail("fake: unknown " + " ".join(a))
"""


class _Router:
    """A router that answers like the engine and records what it was sent."""

    def __init__(self, state: Path, status: int = 200) -> None:
        self.requests: list[tuple[str, int]] = []
        router = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                actor = self.headers["ate-target-actor"]
                router.requests.append((actor, length))
                with open(str(state) + ".consumed", "a") as consumed:
                    consumed.write(actor.split("/")[1] + "\n")
                payload = b'.\n{"results": [{"check_id": "x"}], "errors": []}'
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _latency(tmp_path: Path, template=None, router_status=200, **env):
    fake = tmp_path / "kubectl-ate"
    fake.write_text(f"#!{sys.executable}\n" + _LATENCY_FAKE)
    fake.chmod(0o755)
    path = tmp_path / "template.yaml"
    path.write_text(_LATENCY_TEMPLATE.read_text() if template is None else template)
    state = tmp_path / "state.json"
    router = _Router(state, router_status)
    try:
        run = subprocess.run(
            [sys.executable, "-I", str(_LATENCY)],
            env={
                "ATE_BIN": str(fake),
                "FAKE_STATE": str(state),
                "TEMPLATE_FILE": str(path),
                "ROUTER_URL": router.url,
                "POLL_SECONDS": "0.01",
                "WORKER_WAIT_SECONDS": "2",
                "SEED_WAIT_SECONDS": "2",
                "STATE_WAIT_SECONDS": "5",
                "REQUEST_TIMEOUT_SECONDS": "5",
                "SEQUENTIAL": "3",
                "CONCURRENCY": "2,3",
                "BODY_BYTES": "1024,4096",
                "LARGE_SAMPLES": "2",
                "FLOOR_SAMPLES": "2",
                "ROUNDS": "2",
                **env,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        router.close()
    final = json.loads(state.read_text()) if state.exists() else None
    return run, final, router


def _stats(stdout: str) -> dict[tuple[str, str, str], dict[str, str]]:
    """Map (phase, mode, metric) to the key=value pairs of its stats line."""
    found = {}
    for line in stdout.splitlines():
        if line.startswith("LATENCY ") and "event=stats" in line:
            pairs = dict(p.split("=", 1) for p in line.split()[1:])
            found[(pairs["phase"], pairs["mode"], pairs["metric"])] = pairs
    return found


def _left(state: dict) -> bool:
    return bool(state["atespaces"] or state["templates"] or state["actors"])


def test_latency_control_mode_reports_the_api_path_and_skips_requests(
    tmp_path: Path,
) -> None:
    run, state, router = _latency(tmp_path, MODE="control")
    assert run.returncode == 0, run.stdout + run.stderr
    found = _stats(run.stdout)
    for metric in ("create_ms", "resume_ms", "create_to_ready_ms", "delete_ms"):
        row = found[("control_seq", "control", metric)]
        assert row["n"] == "3" and row["unit"] == "ms"
        assert {"p50", "p95", "p99", "max"} <= set(row)
    # A burst of 3 repeats twice: the stats pool 6 samples, so p95 is not the max
    # of one burst.
    assert found[("control_k3", "control", "create_to_ready_ms")]["n"] == "6"
    assert found[("control_k2", "control", "create_to_ready_ms")]["n"] == "4"
    assert ("floor", "api", "cli_floor_ms") in found
    assert "event=burst k=2" in run.stdout and "per_second=" in run.stdout
    assert "event=data_phase result=skipped reason=MODE=control" in run.stdout
    assert "event=summary result=ok" in run.stdout
    assert router.requests == []
    assert not _left(state)


def test_latency_all_mode_measures_pool_ondemand_and_cold_requests(
    tmp_path: Path,
) -> None:
    run, state, router = _latency(tmp_path, MODE="all")
    assert run.returncode == 0, run.stdout + run.stderr
    found = _stats(run.stdout)
    # The pool: a request to a child that already waits, for both body sizes.
    pool = found[("pool_seq", "pool", "send_to_result_ms")]
    assert pool["n"] in ("2", "3") and pool["body"] in ("1024", "4096")
    assert ("pool_k3", "pool", "send_to_result_ms") in found
    assert ("pool_seq", "pool", "connect_ms") in found
    # On demand: the three numbers the design compares.
    for metric in ("create_to_ready_ms", "send_to_result_ms", "total_ms"):
        assert ("ondemand_k1", "ondemand", metric) in found
        assert ("ondemand_k2", "ondemand", metric) in found
    # Cold: the router resumes the actor, so only the total is meaningful.
    assert ("cold_k1", "cold", "total_ms") in found
    assert ("cold_k3", "cold", "total_ms") in found
    assert ("cold_k1", "cold", "create_to_ready_ms") not in found
    # The request names the actor in the template's atespace and carries the body.
    assert router.requests
    assert all(a.startswith("fork-latency/lat-") for a, _ in router.requests)
    assert {n for _, n in router.requests} == {1024, 4096}
    # Every actor got one request: single use.
    names = [a for a, _ in router.requests]
    assert len(names) == len(set(names))
    assert "event=summary result=ok" in run.stdout
    assert not _left(state)
    # Every API call carries --preview, as the API requires for a fork template.
    assert all(c.startswith("preview ") for c in state["calls"])


def test_latency_refuses_a_placeholder_engine_image_before_any_api_call(
    tmp_path: Path,
) -> None:
    text = re.sub(
        r"@sha256:[0-9a-f]{64}", "@sha256:" + "0" * 64, _LATENCY_TEMPLATE.read_text()
    )
    run, state, _ = _latency(tmp_path, template=text)
    assert run.returncode == 1
    assert "no real sha256 digest" in run.stdout
    assert state is None  # the fake CLI never ran


def test_latency_fails_on_a_router_error_and_leaves_nothing(tmp_path: Path) -> None:
    run, state, _ = _latency(tmp_path, router_status=503, MODE="all")
    assert run.returncode == 1
    assert "HTTP 503" in run.stdout
    assert "event=summary result=fail" in run.stdout
    assert not _left(state)


def test_latency_child_booking_is_the_template_limits_plus_the_overhead() -> None:
    latency = _load_latency()
    cpu, memory = latency.child_booking(_LATENCY_TEMPLATE.read_text())
    template = yaml.safe_load(_LATENCY_TEMPLATE.read_text())
    assert cpu == template["resources"]["limits"]["cpuMillicores"]
    assert memory == template["resources"]["limits"]["memoryBytes"] + (16 << 20)


def test_latency_stops_early_when_the_pool_cannot_book_the_burst(
    tmp_path: Path,
) -> None:
    # 2 CPU is the staged pool: the seed (1000m is allocated) and one child.
    run, state, router = _latency(
        tmp_path, MODE="control", FAKE_CAP_CPU="2000", FAKE_CAP_MEM=str(1280 << 20)
    )
    assert run.returncode == 1
    assert "capacity: 5 children book 5000m CPU" in run.stdout
    assert "event=capacity children=5" in run.stdout
    assert "event=summary result=fail" in run.stdout
    # It stopped before any phase created an actor, and cleaned up.
    assert not [c for c in state["calls"] if c.startswith("preview create actor ")]
    assert not _left(state)


def test_latency_runs_when_the_pool_books_the_burst_and_keeps_a_batch_small(
    tmp_path: Path,
) -> None:
    # Seed 1000m allocated, 9 CPU in all: 8 free covers 3 children of headroom.
    run, state, router = _latency(
        tmp_path,
        MODE="all",
        CONCURRENCY="2",
        POOL_BATCH="2",
        FAKE_CAP_CPU="9000",
        FAKE_CAP_MEM=str(2048 << 20),
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "event=capacity children=3" in run.stdout
    # No phase held more actors than the batch or the burst.
    assert state["peak"] <= 2


def test_latency_skips_the_capacity_check_when_the_worker_reports_none(
    tmp_path: Path,
) -> None:
    run, state, _ = _latency(tmp_path, MODE="control")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "event=capacity result=unknown" in run.stdout


def test_latency_pools_the_rounds_of_a_concurrent_burst(tmp_path: Path) -> None:
    run, state, router = _latency(tmp_path, MODE="all", ROUNDS="3", CONCURRENCY="2")
    assert run.returncode == 0, run.stdout + run.stderr
    found = _stats(run.stdout)
    assert found[("pool_k2", "pool", "send_to_result_ms")]["n"] == "6"
    assert found[("ondemand_k2", "ondemand", "total_ms")]["n"] == "6"
    assert found[("cold_k2", "cold", "total_ms")]["n"] == "6"
    assert run.stdout.count("event=burst k=2 round=") == 3
    assert not _left(state)
