"""Validate the staged gVisor sandbox-fork slice 1: the SandboxConfig pins the
fork build, and the staging Job and its script check what they fetch. The
wired resources' own test (../resources/resources_test.py) checks that none of
this reaches the cluster."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
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


def test_kvm_pool_has_no_workers_and_uses_the_fork_config() -> None:
    pool = _pool()
    assert pool["metadata"]["namespace"] == _NS
    # Off: no pod starts until the enable step raises this.
    assert pool["spec"]["replicas"] == 0
    (klass,) = pool["spec"]["sandboxClasses"]
    assert klass == {
        "name": "gvisor",
        "platform": "kvm",
        "configRef": {"name": "gvisor-fork"},
    }
    # Dedicated: no other pool or SandboxConfig is named, and gvisor-default
    # is not this pool's config.
    assert klass["configRef"]["name"] != "gvisor-default"


def test_kvm_pool_template_sets_only_the_account_and_the_resources() -> None:
    # No tolerations, node selector, volumes, host namespaces or security
    # context: the controller shapes the pod, and a new field is a review item.
    assert set(_pool()["spec"]["template"]) == {"serviceAccountName", "resources"}


def test_kvm_pool_with_workers_has_a_real_worker_image_digest() -> None:
    # The all-zero placeholder is for the staged state only: a pool that
    # starts workers must name a published image.
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


def test_kvm_pool_requests_little_and_limits_for_a_seed_and_16_scans() -> None:
    resources = _pool()["spec"]["template"]["resources"]
    # node-2 had 13Mi left to request on 2026-11-22: keep the reservation at
    # the microVM spike's size.
    assert resources["requests"] == {"cpu": "100m", "memory": "256Mi"}
    # Seed 192 MB + 16 x (37 + 6.5) MB + 64 MB of forkd and worker = 952 MB.
    assert resources["limits"]["memory"] == "1280Mi"
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
    return _one(_docs(), "Job", "gvisor-fork-conformance-1")


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
while a and a[0] in ("--endpoint", "--token-file"):
    a = a[2:]
st["calls"].append(" ".join(a))
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
    if "/" in template and template.split("/")[0] != ns and not env("FAKE_ALLOW_CROSS"):
        fail("rpc error: code = PermissionDenied desc = template is in another atespace")
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
    return _TEMPLATE.read_text().replace("0" * 64, "1" * 64)


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
    assert "create actor fork-3 -a fork-conformance --template engine" in state["calls"]
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
    run, state = _conformance(tmp_path)
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
