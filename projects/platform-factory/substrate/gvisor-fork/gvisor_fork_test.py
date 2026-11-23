"""Validate the staged gVisor sandbox-fork slice 1: the SandboxConfig pins the
fork build, and the staging Job and its script check what they fetch. The
wired resources' own test (../resources/resources_test.py) checks that none of
this reaches the cluster."""

from __future__ import annotations

import hashlib
import io
import json
import os
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
