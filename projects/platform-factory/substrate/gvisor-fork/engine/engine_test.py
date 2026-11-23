"""Tests for the engine image build that need no Docker, no network and no gVisor.

They check the pins, the rules, the layer maker (byte-reproducible, right
files, refuses a wrong wheel), the smoke client's bodies and, when the repository
checkout is visible (a plain `pytest` run from the repository; under Bazel the
runfiles hold no `.github`, so the workflow test skips there). The image itself
is tested by the workflow ("Test the image") and by the fork tests on a node.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
SHA_PIN = re.compile(r"@sha256:[0-9a-f]{64}$")


def pins() -> dict[str, str]:
    out = {}
    for line in (HERE / "pins.env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key] = value
    return out


def test_every_pin_is_exact() -> None:
    p = pins()
    assert SHA_PIN.search(p["BASE_IMAGE"]), "the base image is pinned by digest"
    assert SHA_PIN.search(p["BUILDER_IMAGE"]), "the builder image is pinned by digest"
    assert re.fullmatch(r"[0-9a-f]{64}", p["WHEEL_SHA256"])
    assert re.fullmatch(r"[0-9a-f]{64}", p["CRANE_SHA256"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", p["SEMGREP_VERSION"])
    assert p["WHEEL_URL"].endswith("/" + p["WHEEL_FILE"])
    assert p["WHEEL_URL"].startswith("https://files.pythonhosted.org/packages/")
    assert f"semgrep-{p['SEMGREP_VERSION']}-" in p["WHEEL_FILE"]
    assert "manylinux_2_34_x86_64" in p["WHEEL_FILE"]
    assert p["CRANE_VERSION"] in p["CRANE_URL"]


def test_rules_are_forty_public_regex_rules() -> None:
    rules = json.loads((HERE / "rules.json").read_text())["rules"]
    assert len(rules) == 40
    assert len({r["id"] for r in rules}) == 40
    for rule in rules:
        assert rule["languages"] == ["regex"]
        assert set(rule) == {"id", "languages", "severity", "message", "pattern-regex"}
        re.compile(rule["pattern-regex"])


def test_target_and_launcher_agree() -> None:
    targets = json.loads((HERE / "targets.json").read_text())
    (path,) = [n["path"]["fpath"] for n in targets[1][0][1:]]
    launcher = (HERE / "forksrv-net.c").read_text()
    assert f'"FORKSRV_TARGET", "{path}"' in launcher
    assert "/semgrep/rules.json" in launcher and "/semgrep/targets.json" in launcher
    layer = (HERE / "make-layer.py").read_text()
    assert f'files["{path.lstrip("/")}"]' in layer


def test_server_contract_is_in_the_shim() -> None:
    shim = (HERE / "forksrv-shim.c").read_text()
    assert '"GET /healthz "' in shim, "the wakeup probe path"
    assert '"0.0.0.0:8080"' in shim, "the default port of the template"
    assert "MAX_BODY ((size_t)16 << 20)" in shim
    assert "/proc/gvisor/checkpoint" in shim
    # The probe must not end the server: a healthz answer returns -1 (no scan).
    assert re.search(r'"GET /healthz "[^\n]*reply\(c, "200 OK"[^\n]*return -1', shim)


def test_shell_scripts_parse() -> None:
    for script in ("build.sh", "compile.sh"):
        subprocess.run(["bash", "-n", str(HERE / script)], check=True)


def fake_wheel(version: str) -> bytes:
    prefix = f"semgrep-{version}.data/purelib/semgrep/bin/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(prefix + "semgrep-core", b"core")
        zf.writestr(prefix + "libs/libz.so.1", b"zlib")
        zf.writestr(prefix + "libs/libm.so.6", b"m")
        zf.writestr(f"semgrep-{version}.dist-info/METADATA", b"not in the layer")
    return buf.getvalue()


@pytest.fixture
def maker(tmp_path: Path) -> dict:
    """A copy of the layer maker with pins that name a fake wheel."""
    work = tmp_path / "engine"
    work.mkdir()
    for name in ("make-layer.py", "rules.json", "targets.json"):
        shutil.copy(HERE / name, work / name)
    p = pins()
    wheel = fake_wheel(p["SEMGREP_VERSION"])
    lines = [
        f"{k}={hashlib.sha256(wheel).hexdigest() if k == 'WHEEL_SHA256' else v}"
        for k, v in p.items()
    ]
    (work / "pins.env").write_text("\n".join(lines) + "\n")
    wheel_path = tmp_path / "wheel.whl"
    wheel_path.write_bytes(wheel)
    bins = tmp_path / "bin"
    bins.mkdir()
    for name in ("shim.so", "forksrv-net", "uname"):
        (bins / name).write_bytes(b"binary " + name.encode())
    return {"work": work, "wheel": wheel_path, "bins": bins, "tmp": tmp_path}


def run_maker(m: dict, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(m["work"] / "make-layer.py"),
            str(m["wheel"]),
            str(m["bins"]),
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_layer_is_reproducible_and_complete(maker: dict) -> None:
    first, second = maker["tmp"] / "a.tar", maker["tmp"] / "b.tar"
    assert run_maker(maker, first).returncode == 0
    assert run_maker(maker, second).returncode == 0
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as tar:
        members = {m.name: m for m in tar.getmembers()}
        names = [m.name for m in tar.getmembers()]
        assert names == sorted(names)
        for want in (
            "semgrep/semgrep-core",
            "semgrep/libs/libz.so.1",
            "semgrep/libs/libm.so.6",
            "semgrep/shim.so",
            "semgrep/rules.json",
            "semgrep/targets.json",
            "semgrep/NOTICE",
            "scan/target.json",
            "usr/local/bin/forksrv-net",
            "usr/bin/uname",
        ):
            assert want in members, want
        assert not any("METADATA" in n for n in names)
        for info in members.values():
            assert (info.uid, info.gid, info.mtime) == (0, 0, 0), info.name
            assert info.mode & 0o002 == 0, f"{info.name} is world writable"
        assert members["scan/target.json"].size == 1 << 20
        assert members["usr/local/bin/forksrv-net"].mode == 0o755
        assert members["semgrep/rules.json"].mode == 0o644
        notice = tar.extractfile(members["semgrep/NOTICE"]).read().decode()
        assert "LGPL-2.1-or-later" in notice and "no Semgrep Pro engine" in notice


def test_layer_refuses_a_wheel_that_is_not_the_pinned_one(maker: dict) -> None:
    maker["wheel"].write_bytes(fake_wheel("0.0.0"))
    done = run_maker(maker, maker["tmp"] / "bad.tar")
    assert done.returncode != 0
    assert "is not the pinned" in done.stderr


def test_smoke_bodies_have_the_asked_size_and_both_keys() -> None:
    spec = importlib.util.spec_from_file_location("smoke", HERE / "smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    for size in (1024, 16384, 524288):
        body = smoke.body_of(size)
        assert len(body) == size
        assert body.endswith(smoke.TAIL)
        assert body.count(b"AKIA") == 2


def workflow_path() -> Path | None:
    for parent in HERE.parents:
        candidate = parent / ".github" / "workflows" / "substrate-fork-engine.yml"
        if candidate.is_file():
            return candidate
    return None


def test_workflow_is_manual_and_publishes_only_the_engine_name() -> None:
    path = workflow_path()
    if path is None:
        pytest.skip("the workflow file is outside this test's runfiles")
    text = path.read_text()
    wf = yaml.safe_load(text)
    assert list(wf[True]) == ["workflow_dispatch"], "no push or pull_request trigger"
    assert wf["env"]["IMAGE_REPOSITORY"] == (
        "ghcr.io/jomcgi-org/homelab/substrate-fork/engine-oss"
    )
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["image"]
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    steps = job["steps"]
    for step in steps:
        if "uses" in step:
            assert re.search(r"@[0-9a-f]{40}( #.*)?$", step["uses"]), step["uses"]
        # An input never expands inside a script: it goes through env.
        assert "${{ inputs" not in step.get("run", "")
        assert "secrets." not in str(step)
    # Everything that can write a registry runs only when asked.
    for step in steps:
        run = step.get("run", "")
        if re.search(r"crane auth login|build\.sh push|crane push", run):
            assert step["if"] == (
                "inputs.publish && github.ref == 'refs/heads/main'"
            ), step["name"]
    assert any(
        s.get("if") == "inputs.publish && github.ref != 'refs/heads/main'"
        for s in steps
    ), "a publish from another branch fails loudly"
    # The only push is the one in build.sh, to the one repository.
    assert "docker push" not in text
    assert "docker login" not in text
    assert text.count("build.sh push") == 1
    # It never overwrites a tag, and only "not found" means the tag is free.
    assert "crane digest" in text
    assert "MANIFEST_UNKNOWN|NAME_UNKNOWN" in text
    assert "Cannot tell whether" in text
