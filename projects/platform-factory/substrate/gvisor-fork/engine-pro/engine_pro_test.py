"""Tests for the Pro engine image build that need no Docker, no network and no token.

They check the pins and the shared sources, the layer maker (byte-reproducible, the
placeholder and not the token, right files), the token scan (fake token in a layer,
nested layers, encodings), the privacy guard (stub crane and gh), the fetch script
(a local fake of the two Semgrep endpoints), the smoke client's bodies and, when the
repository checkout is visible (a plain `pytest` run from the repository; under Bazel
the runfiles hold no `.github`, so the workflow tests skip there), the guards of the
workflow. The image itself is tested by the workflow ("Test the image with the
placeholder token"): that needs the Pro engine, which only the token gets.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent / "engine"
FAKE_TOKEN = "fakeTOKEN-0123456789-abcdefghij-not-real"  # a test value, not a token
PLACEHOLDER_MEMBER = "semgrep/.tok"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pins() -> dict[str, str]:
    out = {}
    for path in (ENGINE / "pins.env", HERE / "pins.env"):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                out[key] = value
    return out


# --- pins and shared sources -------------------------------------------------


def test_pro_pins_are_sane_and_the_placeholder_is_not_a_token() -> None:
    p = pins()
    assert p["SEMGREP_URL"].startswith("https://")
    assert p["PRO_PLATFORM"] == "manylinux-x86"
    assert int(p["PRO_BINARY_MIN_BYTES"]) >= 50_000_000
    assert int(p["RULES_MIN_COUNT"]) >= 1000
    placeholder = p["PLACEHOLDER_TOKEN"]
    assert len(placeholder) >= 16 and not re.search(r"\s", placeholder)
    assert "placeholder" in placeholder
    assert re.fullmatch(r"([0-9a-f]{64})?", p["PRO_BINARY_SHA256"])
    # The placeholder is not a secret and the token never goes in a pins file.
    assert not re.fullmatch(r"[0-9a-f]{64}", placeholder)


def test_the_shared_sources_are_the_engine_ones_not_copies() -> None:
    for name in ("forksrv-shim.c", "uname.c", "smoke.py", "edge.py"):
        assert (ENGINE / name).is_file(), name
        assert not (HERE / name).exists(), f"{name} must not be copied here"
    compile_sh = (HERE / "compile.sh").read_text()
    assert '"$engine/forksrv-shim.c"' in compile_sh
    assert '"$engine/uname.c"' in compile_sh
    assert '"$here/forksrv-net.c"' in compile_sh
    # The Pro Semgrep version is the OSS one: one pin.
    assert "SEMGREP_VERSION" not in (HERE / "pins.env").read_text().replace("#", "")


def test_launcher_keeps_the_contract_of_the_oss_launcher() -> None:
    pro = (HERE / "forksrv-net.c").read_text()
    oss = (ENGINE / "forksrv-net.c").read_text()
    for needle in (
        '"--listen"',
        '"0.0.0.0:8080"',
        '"LD_LIBRARY_PATH", "/semgrep/libs"',
        '"LD_PRELOAD", "/semgrep/shim.so"',
        '"FORKSRV_TARGET", "/scan/target.json"',
        '"FORKSRV_LISTEN", listen_spec',
        '"FORKSRV_CHECKPOINT", "1", 0',
    ):
        assert needle in pro and needle in oss, needle
    assert "return 64" in pro
    # What differs: the engine, the flags and the token.
    assert '#define ENGINE_PATH "/semgrep/semgrep-core-proprietary"' in pro
    for flag in ("-secrets", "-disable-secrets-validation", "-pro_intra_file"):
        assert f'"{flag}"' in pro
    assert 'setenv("SEMGREP_APP_TOKEN", token, 1)' in pro
    assert '#define TOKEN_FILE "/semgrep/.tok"' in pro
    # The placeholder value is in pins.env and the layer, not in the launcher.
    assert pins()["PLACEHOLDER_TOKEN"] not in pro


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
def test_launcher_gives_the_engine_the_placeholder_and_the_secrets_flags(
    tmp_path: Path,
) -> None:
    """Compile the launcher with a fake engine and a token file in a temp dir, run it
    with a hostile SEMGREP_APP_TOKEN in its environment, and read what the engine got."""
    fake_engine = tmp_path / "engine.sh"
    fake_engine.write_text(
        '#!/bin/sh\necho "token=$SEMGREP_APP_TOKEN"\necho "args=$*"\n'
        'echo "preload=$LD_PRELOAD target=$FORKSRV_TARGET listen=$FORKSRV_LISTEN cp=$FORKSRV_CHECKPOINT"\n'
    )
    fake_engine.chmod(0o755)
    tok = tmp_path / "tok"
    placeholder = pins()["PLACEHOLDER_TOKEN"]
    tok.write_text(placeholder + "\n")
    binary = tmp_path / "forksrv-net"
    subprocess.run(
        [
            "gcc",
            "-Wall",
            "-Wextra",
            "-Werror",
            f'-DTOKEN_FILE="{tok}"',
            f'-DENGINE_PATH="{fake_engine}"',
            "-o",
            str(binary),
            str(HERE / "forksrv-net.c"),
        ],
        check=True,
    )
    env = {"PATH": os.environ["PATH"], "SEMGREP_APP_TOKEN": FAKE_TOKEN}
    done = subprocess.run(
        [str(binary), "--listen", "0.0.0.0:8080"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert f"token={placeholder}\n" in done.stdout, (
        "the placeholder replaces any inherited token"
    )
    assert FAKE_TOKEN not in done.stdout + done.stderr
    assert "-secrets -disable-secrets-validation -pro_intra_file" in done.stdout
    assert (
        "-rules /semgrep/rules.json -targets /semgrep/targets.json -json -j 1"
        in done.stdout
    )
    assert (
        "preload=/semgrep/shim.so target=/scan/target.json listen=0.0.0.0:8080 cp=1"
        in done.stdout
    )
    # A missing or empty token file is exit 78 and no engine start; a bad flag is 64.
    tok.write_text("\n")
    done = subprocess.run(
        [str(binary)], capture_output=True, text=True, env=env, check=False
    )
    assert done.returncode == 78 and "token=" not in done.stdout
    tok.unlink()
    assert (
        subprocess.run(
            [str(binary)], capture_output=True, env=env, check=False
        ).returncode
        == 78
    )
    assert (
        subprocess.run(
            [str(binary), "--bad"], capture_output=True, env=env, check=False
        ).returncode
        == 64
    )


def test_targets_name_the_secrets_product_and_the_same_path() -> None:
    pro = json.loads((HERE / "targets.json").read_text())
    oss = json.loads((ENGINE / "targets.json").read_text())
    assert pro[1][0][1]["products"] == ["secrets"]
    assert pro[1][0][1]["path"] == oss[1][0][1]["path"]
    assert pro[1][0][1]["path"]["fpath"] == "/scan/target.json"


def test_shell_scripts_parse() -> None:
    for script in ("build.sh", "compile.sh", "check-private.sh", "canary.sh"):
        subprocess.run(["bash", "-n", str(HERE / script)], check=True)


def test_build_refuses_to_run_with_the_token() -> None:
    env = dict(os.environ, SEMGREP_APP_TOKEN=FAKE_TOKEN)
    done = subprocess.run(
        ["bash", str(HERE / "build.sh"), "layer", str(HERE / "no-such-out")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert done.returncode == 1
    assert "SEMGREP_APP_TOKEN is set" in done.stderr
    assert FAKE_TOKEN not in done.stdout + done.stderr
    assert not (HERE / "no-such-out").exists()


# --- the layer maker ---------------------------------------------------------


def fake_wheel(version: str) -> bytes:
    prefix = f"semgrep-{version}.data/purelib/semgrep/bin/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(prefix + "semgrep-core", b"oss core, not in the Pro layer")
        zf.writestr(prefix + "libs/libz.so.1", b"zlib")
        zf.writestr(prefix + "libs/libm.so.6", b"m")
        zf.writestr(f"semgrep-{version}.dist-info/METADATA", b"not in the layer")
    return buf.getvalue()


def fake_rules(count: int) -> bytes:
    rules = [{"id": f"rule.{i:04d}", "languages": ["regex"]} for i in range(count)]
    return json.dumps({"rules": rules}).encode()


@pytest.fixture
def maker(tmp_path: Path) -> dict:
    """A copy of both pins files and the layer maker that name a fake wheel, a tiny
    "engine" and 1000 rules."""
    root = tmp_path / "gvisor-fork"
    (root / "engine").mkdir(parents=True)
    work = root / "engine-pro"
    work.mkdir()
    p = pins()
    wheel = fake_wheel(p["SEMGREP_VERSION"])
    oss_lines = [
        f"{k}={hashlib.sha256(wheel).hexdigest() if k == 'WHEEL_SHA256' else v}"
        for k, v in pins_of(ENGINE / "pins.env").items()
    ]
    (root / "engine" / "pins.env").write_text("\n".join(oss_lines) + "\n")
    pro_lines = [
        f"{k}={'100' if k == 'PRO_BINARY_MIN_BYTES' else v}"
        for k, v in pins_of(HERE / "pins.env").items()
    ]
    (work / "pins.env").write_text("\n".join(pro_lines) + "\n")
    for name in ("make-layer.py", "targets.json"):
        shutil.copy(HERE / name, work / name)
    wheel_path = tmp_path / "wheel.whl"
    wheel_path.write_bytes(wheel)
    bins = tmp_path / "bin"
    bins.mkdir()
    for name in ("shim.so", "forksrv-net", "uname"):
        (bins / name).write_bytes(b"binary " + name.encode())
    fetched = tmp_path / "pro"
    fetched.mkdir()
    (fetched / "semgrep-core-proprietary").write_bytes(b"\x7fELF" + b"pro" * 100)
    (fetched / "rules.json").write_bytes(fake_rules(1000))
    return {
        "work": work,
        "wheel": wheel_path,
        "bins": bins,
        "pro": fetched,
        "tmp": tmp_path,
    }


def pins_of(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key] = value
    return out


def run_maker(
    m: dict, out: Path, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(m["work"] / "make-layer.py"),
            str(m["wheel"]),
            str(m["bins"]),
            str(m["pro"]),
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k != "SEMGREP_APP_TOKEN"}


def test_layer_is_reproducible_complete_and_holds_the_placeholder(maker: dict) -> None:
    first, second = maker["tmp"] / "a.tar", maker["tmp"] / "b.tar"
    assert run_maker(maker, first, clean_env()).returncode == 0
    assert run_maker(maker, second, clean_env()).returncode == 0
    assert first.read_bytes() == second.read_bytes()
    placeholder = pins()["PLACEHOLDER_TOKEN"]
    with tarfile.open(first) as tar:
        members = {m.name: m for m in tar.getmembers()}
        names = [m.name for m in tar.getmembers()]
        assert names == sorted(names)
        for want in (
            "semgrep/semgrep-core-proprietary",
            "semgrep/libs/libz.so.1",
            "semgrep/libs/libm.so.6",
            "semgrep/shim.so",
            "semgrep/rules.json",
            "semgrep/targets.json",
            "semgrep/.tok",
            "semgrep/BUILD-INFO.json",
            "semgrep/NOTICE",
            "scan/target.json",
            "usr/local/bin/forksrv-net",
            "usr/bin/uname",
        ):
            assert want in members, want
        assert "semgrep/semgrep-core" not in members, (
            "the OSS core is not in this image"
        )
        assert not any("METADATA" in n for n in names)
        for info in members.values():
            assert (info.uid, info.gid, info.mtime) == (0, 0, 0), info.name
            assert info.mode & 0o002 == 0, f"{info.name} is world writable"
        assert members["scan/target.json"].size == 1 << 20
        assert members["usr/local/bin/forksrv-net"].mode == 0o755
        assert members["semgrep/semgrep-core-proprietary"].mode == 0o755
        assert members["semgrep/.tok"].mode == 0o444
        assert (
            tar.extractfile(members["semgrep/.tok"]).read()
            == placeholder.encode() + b"\n"
        )
        info = json.loads(tar.extractfile(members["semgrep/BUILD-INFO.json"]).read())
        assert (
            info["rules_count"] == 1000
            and "real token is not in this image" in info["token"]
        )
        notice = tar.extractfile(members["semgrep/NOTICE"]).read().decode()
        assert "PRIVATE IMAGE" in notice and "placeholder" in notice
        # The placeholder is in /semgrep/.tok and nowhere else.
        for name, info in members.items():
            if name != "semgrep/.tok" and info.isfile():
                assert placeholder.encode() not in tar.extractfile(info).read(), name


def test_layer_refuses_the_token_in_its_environment(maker: dict) -> None:
    env = dict(os.environ, SEMGREP_APP_TOKEN=FAKE_TOKEN)
    done = run_maker(maker, maker["tmp"] / "x.tar", env)
    assert done.returncode != 0
    assert "SEMGREP_APP_TOKEN is set" in done.stderr
    assert FAKE_TOKEN not in done.stdout + done.stderr


def test_layer_refuses_bad_inputs(maker: dict) -> None:
    maker["wheel"].write_bytes(fake_wheel("0.0.0"))
    done = run_maker(maker, maker["tmp"] / "bad.tar", clean_env())
    assert done.returncode != 0 and "is not the pinned" in done.stderr


def test_layer_refuses_a_small_rule_set_and_a_non_elf_engine(maker: dict) -> None:
    (maker["pro"] / "rules.json").write_bytes(fake_rules(10))
    done = run_maker(maker, maker["tmp"] / "a.tar", clean_env())
    assert done.returncode != 0 and "below" in done.stderr
    (maker["pro"] / "rules.json").write_bytes(fake_rules(1000))
    (maker["pro"] / "semgrep-core-proprietary").write_bytes(b"#!/bin/sh\n" * 100)
    done = run_maker(maker, maker["tmp"] / "b.tar", clean_env())
    assert done.returncode != 0 and "not an ELF" in done.stderr


# --- the token scan ----------------------------------------------------------


@pytest.fixture(scope="module")
def scan_tool():
    return HERE / "token-scan.py"


def run_scan(
    tool: Path, *paths: Path, token: str | None = FAKE_TOKEN, extra=()
) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "SEMGREP_APP_TOKEN"}
    if token is not None:
        env["SEMGREP_APP_TOKEN"] = token
    placeholder = pins()["PLACEHOLDER_TOKEN"]
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(tool),
            "--placeholder",
            placeholder,
            "--placeholder-member",
            PLACEHOLDER_MEMBER,
            *extra,
            *map(str, paths),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def make_tar(path: Path, members: dict[str, bytes], gz: bool = False) -> Path:
    mode = "w:gz" if gz else "w"
    with tarfile.open(path, mode) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def clean_members() -> dict[str, bytes]:
    return {
        "semgrep/semgrep-core-proprietary": b"\x7fELF" + b"engine " * 1000,
        PLACEHOLDER_MEMBER: pins()["PLACEHOLDER_TOKEN"].encode() + b"\n",
        "semgrep/rules.json": b'{"rules": []}',
    }


def test_scan_passes_a_clean_layer(scan_tool, tmp_path: Path) -> None:
    layer = make_tar(tmp_path / "layer.tar", clean_members())
    done = run_scan(scan_tool, layer)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "clean" in done.stdout


def test_scan_fails_on_the_token_in_a_layer_and_never_prints_it(
    scan_tool, tmp_path: Path
) -> None:
    members = clean_members()
    members["semgrep/rules.json"] = (
        b'{"rules": [], "note": "' + FAKE_TOKEN.encode() + b'"}'
    )
    layer = make_tar(tmp_path / "layer.tar", members)
    done = run_scan(scan_tool, layer)
    assert done.returncode == 1
    assert "TOKEN (plain)" in done.stdout and "semgrep/rules.json" in done.stdout
    assert FAKE_TOKEN not in done.stdout + done.stderr
    assert FAKE_TOKEN[:12] not in done.stdout + done.stderr


@pytest.mark.parametrize(
    "kind,encode",
    [
        ("reversed", lambda t: t[::-1]),
        ("hex", lambda t: t.hex().encode()),
        ("base64", lambda t: base64.b64encode(t)),
        ("base64+1", lambda t: base64.b64encode(b"x" + t)),
        ("base64+2", lambda t: base64.b64encode(b"xy" + t)),
        ("base64url", lambda t: base64.urlsafe_b64encode(t)),
    ],
)
def test_scan_finds_the_token_in_an_encoded_form(
    scan_tool, tmp_path: Path, kind, encode
) -> None:
    members = clean_members()
    members["semgrep/rules.json"] = b"before " + encode(FAKE_TOKEN.encode()) + b" after"
    done = run_scan(scan_tool, make_tar(tmp_path / "layer.tar", members))
    assert done.returncode == 1, kind
    assert "TOKEN (" in done.stdout


def test_scan_finds_the_token_in_a_layer_inside_an_image(
    scan_tool, tmp_path: Path
) -> None:
    members = clean_members()
    members["etc/leak"] = b"x" * 5000 + FAKE_TOKEN.encode()
    layer = make_tar(tmp_path / "layer.tar", members)
    inner = layer.read_bytes()
    image = make_tar(
        tmp_path / "image.tar",
        {
            "manifest.json": b"[]",
            "abc123.tar.gz": gzip.compress(inner),
            "def456.tar": clean_tar_bytes(tmp_path),
        },
    )
    done = run_scan(scan_tool, image)
    assert done.returncode == 1
    assert "abc123.tar.gz" in done.stdout and "etc/leak" in done.stdout
    # The same image without the leak is clean and shows the placeholder.
    clean_layer = make_tar(tmp_path / "clean-layer.tar", clean_members())
    ok_image = make_tar(
        tmp_path / "ok-image.tar",
        {"manifest.json": b"[]", "abc.tar.gz": gzip.compress(clean_layer.read_bytes())},
    )
    done = run_scan(scan_tool, ok_image)
    assert done.returncode == 0, done.stdout


def clean_tar_bytes(tmp_path: Path) -> bytes:
    return make_tar(
        tmp_path / "other.tar", {"etc/passwd": b"root:x:0:0\n"}
    ).read_bytes()


def test_scan_finds_the_token_across_a_chunk_boundary(
    scan_tool, tmp_path: Path
) -> None:
    chunk = 1 << 20
    data = b"a" * (chunk - 10) + FAKE_TOKEN.encode() + b"b" * 100
    members = clean_members()
    members["semgrep/big"] = data
    done = run_scan(scan_tool, make_tar(tmp_path / "layer.tar", members))
    assert done.returncode == 1 and "TOKEN (plain)" in done.stdout


def test_scan_requires_the_placeholder_only_in_its_member(
    scan_tool, tmp_path: Path
) -> None:
    placeholder = pins()["PLACEHOLDER_TOKEN"].encode()
    members = clean_members()
    members["semgrep/other"] = b"x" + placeholder
    done = run_scan(scan_tool, make_tar(tmp_path / "a.tar", members))
    assert done.returncode == 1 and "PLACEHOLDER outside semgrep/.tok" in done.stdout
    members = clean_members()
    del members[PLACEHOLDER_MEMBER]
    done = run_scan(scan_tool, make_tar(tmp_path / "b.tar", members))
    assert (
        done.returncode == 1 and "the placeholder is not in semgrep/.tok" in done.stdout
    )
    done = run_scan(scan_tool, tmp_path / "b.tar", extra=("--no-require-placeholder",))
    assert done.returncode == 0


def test_scan_reads_a_directory_and_bad_use_is_exit_2(
    scan_tool, tmp_path: Path
) -> None:
    (tmp_path / "pro").mkdir()
    (tmp_path / "pro" / "rules.json").write_text("{}")
    assert (
        run_scan(
            scan_tool, tmp_path / "pro", extra=("--no-require-placeholder",)
        ).returncode
        == 0
    )
    (tmp_path / "pro" / "leak").write_text("x" + FAKE_TOKEN)
    assert (
        run_scan(
            scan_tool, tmp_path / "pro", extra=("--no-require-placeholder",)
        ).returncode
        == 1
    )
    assert run_scan(scan_tool, tmp_path / "pro", token=None).returncode == 2
    assert run_scan(scan_tool, tmp_path / "pro", token="short").returncode == 2
    assert (
        run_scan(
            scan_tool, tmp_path / "pro", token=pins()["PLACEHOLDER_TOKEN"]
        ).returncode
        == 2
    )
    assert run_scan(scan_tool, tmp_path / "missing").returncode == 2


# --- the privacy guard (stub crane and gh) -----------------------------------

REF = "ghcr.io/jomcgi-org/homelab/substrate-fork/engine-pro:1.186.0-abcdef123456"


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    """Stub crane and gh. The stub crane tells the two calls of the guard apart: the
    anonymous one has DOCKER_CONFIG set (an empty dir) and nothing else in its
    environment; the authenticated one has neither. Each call has its own mode file:
    `auth` (the call with the login) and `mode` (the anonymous call)."""
    bin_dir = tmp_path / "stubs"
    bin_dir.mkdir()
    (bin_dir / "crane").write_text(
        "#!/usr/bin/env bash\n"
        'dir=$(dirname "$0")\n'
        'if [[ -n "${DOCKER_CONFIG:-}" ]]; then kind=anon; mode=$(cat "$dir/mode"); '
        'else kind=auth; mode=$(cat "$dir/auth"); fi\n'
        'echo "crane-$kind $*" >>"$dir/log"\n'
        "# the anonymous call must have no token and no inherited env\n"
        'if [[ $kind == anon ]]; then env | grep -q -E "^(GH_TOKEN|GITHUB_TOKEN)=" && echo LEAK >>"$dir/log"; fi\n'
        'case "$mode" in\n'
        "  open) echo '{\"schemaVersion\": 2}'; exit 0 ;;\n"
        '  denied) echo "Error: fetching manifest x/y:t: GET https://ghcr.io/v2/x/y/manifests/t: UNAUTHORIZED: authentication required" >&2; exit 1 ;;\n'
        '  denied403) echo "Error: fetching manifest x/y:t: GET https://ghcr.io/v2/x/y/manifests/t: DENIED: denied" >&2; exit 1 ;;\n'
        '  notfound) echo "Error: fetching manifest x/y:t: GET https://ghcr.io/v2/x/y/manifests/t: NAME_UNKNOWN: repo not found" >&2; exit 1 ;;\n'
        '  manifest_unknown) echo "Error: fetching manifest x/y:t: GET https://ghcr.io/v2/x/y/manifests/t: MANIFEST_UNKNOWN: manifest unknown" >&2; exit 1 ;;\n'
        '  plain404) echo "Error: fetching manifest x/y:t: GET https://ghcr.io/v2/x/y/manifests/t: unexpected status code 404 Not Found: x" >&2; exit 1 ;;\n'
        '  plain403) echo "Error: fetching manifest x/y:t: GET https://ghcr.io/v2/x/y/manifests/t: unexpected status code 403 Forbidden: x" >&2; exit 1 ;;\n'
        '  token403) echo "Error: fetching manifest x/y:t: GET https://ghcr.io/token?scope=repository%3Ax%2Fy%3Apull&service=ghcr.io: unexpected status code 403 Forbidden: x" >&2; exit 1 ;;\n'
        '  token_denied) echo "Error: fetching manifest x/y:t: GET https://ghcr.io/token?scope=repository%3Ax%2Fy%3Apull&service=ghcr.io: DENIED: requested access to the resource is denied" >&2; exit 1 ;;\n'
        '  token_unauthorized) echo "Error: fetching manifest x/y:t: GET https://ghcr.io/token?scope=repository%3Ax%2Fy%3Apull&service=ghcr.io: UNAUTHORIZED: authentication required" >&2; exit 1 ;;\n'
        '  token429) echo "Error: fetching manifest x/y:t: GET https://ghcr.io/token?scope=repository%3Ax%2Fy%3Apull&service=ghcr.io: unexpected status code 429 Too Many Requests: x" >&2; exit 1 ;;\n'
        '  token500) echo "Error: fetching manifest x/y:t: GET https://ghcr.io/token?scope=repository%3Ax%2Fy%3Apull&service=ghcr.io: unexpected status code 500 Internal Server Error: x" >&2; exit 1 ;;\n'
        '  unknown) echo "Error: dial tcp: i/o timeout" >&2; exit 1 ;;\n'
        "esac\n"
    )
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'dir=$(dirname "$0")\n'
        'echo "gh $*" >>"$dir/log"\n'
        'if [[ "$*" == *versions\\?per_page* ]]; then\n'
        '  [[ -f "$dir/no-version" ]] && exit 0\n'
        "  echo 424242\n"
        "fi\n"
        '[[ -f "$dir/delete-fails" && "$*" == *DELETE* ]] && exit 1\n'
        "exit 0\n"
    )
    for name in ("crane", "gh"):
        (bin_dir / name).chmod(0o755)
    (bin_dir / "log").write_text("")
    return bin_dir


def run_guard(
    stubs: Path,
    mode: str,
    ref: str = REF,
    auth: str = "open",
    flags: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess, str]:
    (stubs / "mode").write_text(mode)
    (stubs / "auth").write_text(auth)
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(stubs),
        "GH_TOKEN": "gh-token-for-delete-only",
        "CHECK_PRIVATE_SLEEP": "0",
    }
    done = subprocess.run(
        ["bash", str(HERE / "check-private.sh"), *flags, ref],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return done, (stubs / "log").read_text()


@pytest.mark.parametrize(
    "mode", ["denied", "denied403", "token_unauthorized", "token_denied"]
)
def test_guard_passes_when_the_image_resolves_and_the_anonymous_pull_is_refused(
    stubs: Path, mode: str
) -> None:
    done, log = run_guard(stubs, mode)
    assert done.returncode == 0, done.stderr
    assert "is private" in done.stdout
    # The authenticated call comes first, then the anonymous one.
    assert log.index("crane-auth manifest") < log.index("crane-anon manifest")
    assert "gh " not in log, "nothing is deleted"
    assert "LEAK" not in log, "crane saw a token in its environment"


@pytest.mark.parametrize("auth", ["denied", "notfound", "unknown"])
def test_guard_fails_when_the_image_does_not_resolve_with_the_login(
    stubs: Path, auth: str
) -> None:
    """A package that does not exist is refused like a private one. Without the
    authenticated proof the anonymous refusal means nothing."""
    done, log = run_guard(stubs, "denied", auth=auth)
    assert done.returncode == 1
    assert "does not resolve with the workflow's login" in done.stderr
    assert "NOT proven private" in done.stderr
    assert log.count("crane-auth manifest") == 3, "three tries"
    assert "crane-anon" not in log, "no anonymous call after a failed proof"
    assert "gh " not in log, "no delete: there is nothing to delete"


def test_guard_deletes_and_fails_when_the_image_is_public(stubs: Path) -> None:
    done, log = run_guard(stubs, "open")
    assert done.returncode == 1
    assert "NOT refused" in done.stderr
    assert "crane-anon manifest " + REF in log
    assert (
        "gh api --paginate /orgs/jomcgi-org/packages/container/homelab%2Fsubstrate-fork%2Fengine-pro/versions"
        in log
    )
    assert (
        "gh api --method DELETE /orgs/jomcgi-org/packages/container/"
        "homelab%2Fsubstrate-fork%2Fengine-pro/versions/424242"
    ) in log
    assert "LEAK" not in log


@pytest.mark.parametrize(
    "mode",
    [
        "notfound",
        "manifest_unknown",
        "plain404",
        "plain403",
        "token403",
        "token429",
        "token500",
        "unknown",
    ],
)
def test_guard_does_not_count_a_404_or_a_token_endpoint_error_as_a_refusal(
    stubs: Path, mode: str
) -> None:
    """A public package can answer 404 or MANIFEST_UNKNOWN to one call (a stale negative
    cache, a flaky read), and the token endpoint can answer 403, 429 or 5xx with no
    registry error code (a rate limit, an outage). None of these says that the package
    is private: three tries, then the job fails."""
    done, log = run_guard(stubs, mode)
    assert done.returncode == 1, done.stdout
    assert "is private" not in done.stdout
    assert log.count("crane-anon manifest") == 3, "three tries, not a pass"
    assert "no clear answer" in done.stderr
    assert "DELETE" in log, (
        "the Pro image version is deleted when it is not proven private"
    )


# The answers of ghcr.io to an anonymous `crane manifest`, as a review saw them on
# private first-party packages and on a name that does not exist. A private package
# is refused at the TOKEN endpoint, not at /manifests/.
REAL_GHCR_PRIVATE = (
    "Error: fetching manifest ghcr.io/jomcgi-org/homelab/projects/factory/broker:latest: "
    "GET https://ghcr.io/token?scope=repository%3Ajomcgi-org%2Fhomelab%2Fprojects%2Ffactory%2Fbroker%3Apull"
    "&service=ghcr.io: UNAUTHORIZED: authentication required"
)
REAL_GHCR_NO_SUCH_NAME = (
    "Error: fetching manifest ghcr.io/jomcgi-org/homelab/nothing-here:latest: "
    "GET https://ghcr.io/token?scope=repository%3Ajomcgi-org%2Fhomelab%2Fnothing-here%3Apull"
    "&service=ghcr.io: DENIED: requested access to the resource is denied"
)


@pytest.mark.parametrize("answer", [REAL_GHCR_PRIVATE, REAL_GHCR_NO_SUCH_NAME])
def test_guard_matches_the_exact_strings_of_ghcr_for_a_refused_anonymous_pull(
    tmp_path: Path, answer: str
) -> None:
    """Feed the real strings to the matching step. Step 1 (the authenticated resolve)
    has already shown that the image exists, so DENIED at the token endpoint is sound."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "crane").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ -n "${DOCKER_CONFIG:-}" ]]; then\n'
        '  cat "$(dirname "$0")/answer" >&2; exit 1\n'
        "fi\n"
        "echo '{}'\n"
    )
    (stubs / "answer").write_text(answer + "\n")  # the anonymous call has an empty env
    (stubs / "gh").write_text("#!/usr/bin/env bash\nexit 99\n")
    for name in ("crane", "gh"):
        (stubs / name).chmod(0o755)
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(stubs),
        "CHECK_PRIVATE_SLEEP": "0",
    }
    done = subprocess.run(
        ["bash", str(HERE / "check-private.sh"), REF],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "is private" in done.stdout


def test_guard_with_no_delete_fails_without_calling_gh(stubs: Path) -> None:
    """The canary holds no proprietary byte: a public package is fixed in the
    package settings, not by deleting a version."""
    done, log = run_guard(stubs, "open", flags=("--no-delete",))
    assert done.returncode == 1
    assert "Set the package private" in done.stderr
    assert "gh " not in log


def test_guard_treats_an_unclear_answer_as_not_private(stubs: Path) -> None:
    done, log = run_guard(stubs, "unknown")
    assert done.returncode == 1
    assert log.count("crane-anon manifest") == 3, "three tries"
    assert "DELETE" in log


def test_guard_says_so_when_it_cannot_delete(stubs: Path) -> None:
    (stubs / "delete-fails").write_text("")
    done, _ = run_guard(stubs, "open")
    assert done.returncode == 1 and "DELETE IT BY HAND" in done.stderr
    assert "package admin" in done.stderr and "5000" in done.stderr
    (stubs / "delete-fails").unlink()
    (stubs / "no-version").write_text("")
    done, _ = run_guard(stubs, "open")
    assert done.returncode == 1 and "DELETE" in done.stderr and "BY HAND" in done.stderr


def test_guard_rejects_a_reference_it_does_not_understand(stubs: Path) -> None:
    done, _ = run_guard(stubs, "denied", "docker.io/library/x:1")
    assert done.returncode == 64


# --- the guard against a fake registry, with the real crane ------------------

FAKE_REGISTRY = r"""
import base64, http.server, json, os, sys, threading
MODE = {"m": "unauthorized"}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body=b"", extra=()):
        self.send_response(code)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def err(self, status, code):
        body = json.dumps({"errors": [{"code": code, "message": "x"}]}).encode()
        self.send(status, body, [("Content-Type", "application/json")])

    def do_GET(self):
        port = self.server.server_port
        auth = self.headers.get("Authorization", "")
        m = MODE["m"]
        if self.path.startswith("/token"):
            if auth.startswith("Basic "):
                return self.send(200, b'{"token":"ok"}')
            if m == "token403":
                return self.send(403, b"forbidden")
            if m == "token429":
                return self.send(429, b"slow down")
            if m == "token500":
                return self.send(500, b"oops")
            if m == "token_unauthorized":
                return self.err(401, "UNAUTHORIZED")
            if m == "token_denied":
                return self.err(403, "DENIED")
            return self.send(200, b'{"token":"anon"}')
        if self.path == "/v2/":
            if auth in ("Bearer ok", "Bearer anon"):
                return self.send(200, b"{}")
            realm = 'Bearer realm="http://127.0.0.1:%d/token",service="reg"' % port
            return self.send(401, b"", [("WWW-Authenticate", realm)])
        if "/manifests/" in self.path:
            if auth == "Bearer ok" or m == "public":
                return self.send(200, b'{"schemaVersion":2,"mediaType":"application/vnd.docker.distribution.manifest.v2+json"}', [("Content-Type", "application/vnd.docker.distribution.manifest.v2+json")])
            table = {"unauthorized": (401, "UNAUTHORIZED"), "denied": (403, "DENIED"),
                     "manifest_unknown": (404, "MANIFEST_UNKNOWN"), "name_unknown": (404, "NAME_UNKNOWN")}
            if m in table:
                return self.err(*table[m])
            if m == "plain404":
                return self.send(404, b"not found")
            if m == "plain403":
                return self.send(403, b"nope")
        return self.send(404, b"")


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
cfg = sys.argv[1]
os.makedirs(cfg, exist_ok=True)
auth = base64.b64encode(b"u:p").decode()
open(cfg + "/config.json", "w").write(json.dumps({"auths": {"127.0.0.1:%d" % server.server_port: {"auth": auth}}}))
print(server.server_port, flush=True)


def read_modes():
    for line in sys.stdin:
        MODE["m"] = line.strip()
        print("ok", flush=True)


threading.Thread(target=read_modes, daemon=True).start()
server.serve_forever()
"""


@pytest.fixture
def fake_registry(tmp_path: Path):
    """A registry that answers like GHCR to a client with the login and, by mode, in
    several ways to an anonymous one. Yields (port, docker_config_dir, set_mode)."""
    if shutil.which("crane") is None:
        pytest.skip("no crane")
    script = tmp_path / "fakereg.py"
    script.write_text(FAKE_REGISTRY)
    cfg = tmp_path / "dockercfg"
    proc = subprocess.Popen(
        [sys.executable, "-I", str(script), str(cfg)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    port = int(proc.stdout.readline())

    def set_mode(mode: str) -> None:
        proc.stdin.write(mode + "\n")
        proc.stdin.flush()
        assert proc.stdout.readline().strip() == "ok"

    try:
        yield port, cfg, set_mode
    finally:
        proc.terminate()
        proc.wait()


def run_guard_real(
    tmp_path: Path, port: int, cfg: Path, flags: tuple[str, ...] = ()
) -> tuple[subprocess.CompletedProcess, str]:
    stubs = tmp_path / "ghstub"
    stubs.mkdir(exist_ok=True)
    (stubs / "gh").write_text(
        '#!/usr/bin/env bash\necho "gh $*" >>"$(dirname "$0")/log"\n'
        '[[ "$*" == *versions\\?per_page* ]] && echo 424242\nexit 0\n'
    )
    (stubs / "gh").chmod(0o755)
    (stubs / "log").write_text("")
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(stubs),
        "DOCKER_CONFIG": str(cfg),
        "GH_TOKEN": "gh-token-for-delete-only",
        "CHECK_PRIVATE_SLEEP": "0",
    }
    ref = f"127.0.0.1:{port}/jomcgi-org/homelab/substrate-fork/engine-pro:1.186.0-abcdef123456"
    done = subprocess.run(
        ["bash", str(HERE / "check-private.sh"), *flags, ref],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return done, (stubs / "log").read_text()


@pytest.mark.parametrize(
    "mode", ["unauthorized", "denied", "token_unauthorized", "token_denied"]
)
def test_real_crane_a_registry_error_code_of_the_manifest_or_the_token_endpoint_is_a_refusal(
    tmp_path: Path, fake_registry, mode: str
) -> None:
    port, cfg, set_mode = fake_registry
    set_mode(mode)
    done, log = run_guard_real(tmp_path, port, cfg)
    assert done.returncode == 0, done.stderr
    assert "is private" in done.stdout
    assert "gh " not in log


@pytest.mark.parametrize(
    "mode",
    [
        "manifest_unknown",
        "name_unknown",
        "plain404",
        "plain403",
        "token403",
        "token429",
        "token500",
        "public",
    ],
)
def test_real_crane_everything_else_fails_the_gate(
    tmp_path: Path, fake_registry, mode: str
) -> None:
    """404, MANIFEST_UNKNOWN, NAME_UNKNOWN, a 403 without a registry error code, a 403,
    429 or 5xx of the token endpoint with no registry error code, and a public manifest:
    none is proof that the package is private."""
    port, cfg, set_mode = fake_registry
    set_mode(mode)
    done, log = run_guard_real(tmp_path, port, cfg)
    assert done.returncode == 1, (mode, done.stdout, done.stderr)
    assert "is private" not in done.stdout
    if mode == "public":
        assert "NOT refused" in done.stderr
    else:
        assert "no clear answer" in done.stderr
    assert "DELETE" in log


# --- the canary --------------------------------------------------------------


@pytest.mark.skipif(shutil.which("crane") is None, reason="no crane")
def test_canary_is_content_free_and_labelled(tmp_path: Path) -> None:
    """Push the canary to a local registry (`crane registry serve`) and read it back."""
    import socket
    import time

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(
        ["crane", "registry", "serve", "--address", f"127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ref = f"127.0.0.1:{port}/x/engine-pro:canary-test"
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        done = subprocess.run(
            ["bash", str(HERE / "canary.sh"), ref],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
        )
        assert done.returncode == 0, done.stderr
        manifest = json.loads(
            subprocess.run(
                ["crane", "manifest", ref], capture_output=True, check=True
            ).stdout
        )
        config = json.loads(
            subprocess.run(
                ["crane", "config", ref], capture_output=True, check=True
            ).stdout
        )
        assert "PRIVATE-CANARY" in config["config"]["Labels"]
        assert len(manifest["layers"]) == 1 and manifest["layers"][0]["size"] < 1024
        layer = subprocess.run(
            [
                "crane",
                "blob",
                f"127.0.0.1:{port}/x/engine-pro@{manifest['layers'][0]['digest']}",
            ],
            capture_output=True,
            check=True,
        ).stdout
        tar_bytes = gzip.decompress(layer)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            assert tar.getmembers() == [], "the canary layer has no member"
    finally:
        server.terminate()
        server.wait()


def test_canary_script_never_reads_the_build_directory_or_the_token() -> None:
    script = (HERE / "canary.sh").read_text()
    for needle in (
        "RUNNER_TEMP",
        "layer.tar",
        "image.tar",
        "/pro",
        "SEMGREP_APP_TOKEN",
        "semgrep-core",
        "rules.json",
    ):
        assert needle not in script.replace("# ", ""), needle


# --- the fetch script, against a local fake of the two endpoints --------------


class FakeSemgrep(http.server.BaseHTTPRequestHandler):
    calls: list[tuple[str, str]] = []
    binary = b"\x7fELF" + b"p" * 2000

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        FakeSemgrep.calls.append((self.path, self.headers.get("Authorization", "")))
        if self.headers.get("Authorization") != f"Bearer {FAKE_TOKEN}":
            self.send_response(401)
            self.end_headers()
            return
        if self.path.startswith(
            "/api/agent/deployments/deepbinary/manylinux-x86?version="
        ):
            body, kind = self.binary, "application/octet-stream"
        elif self.path.startswith("/api/agent/deployments/scans/config?"):
            rules = [{"id": f"r{i}", "languages": ["regex"]} for i in (3, 1, 2)]
            body = json.dumps({"rule_config": json.dumps({"rules": rules})}).encode()
            kind = "application/json"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_fetch_downloads_with_the_header_only_and_writes_no_token(
    tmp_path: Path,
) -> None:
    fetch = load(HERE / "fetch-pro.py", "fetch_pro")
    server = http.server.HTTPServer(("127.0.0.1", 0), FakeSemgrep)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The script speaks https only: check that, then call its functions over
        # plain http by calling the lower level pieces with the fake base URL.
        with pytest.raises(SystemExit, match="not https"):
            fetch.open_url(
                f"http://127.0.0.1:{server.server_port}/x", FAKE_TOKEN, "*/*"
            )
        base = f"http://127.0.0.1:{server.server_port}"
        original = fetch.open_url

        def http_open(url: str, token: str, accept: str):
            import urllib.request

            req = urllib.request.Request(url, headers={"Accept": accept})
            req.add_unredirected_header("Authorization", f"Bearer {token}")
            return urllib.request.build_opener(fetch.KeepAuthOnSameHost).open(
                req, timeout=10
            )

        fetch.open_url = http_open
        sha, size = fetch.download_binary(
            base, "manylinux-x86", "1.186.0", FAKE_TOKEN, tmp_path / "engine"
        )
        assert size == len(FakeSemgrep.binary)
        assert sha == hashlib.sha256(FakeSemgrep.binary).hexdigest()
        assert (tmp_path / "engine").stat().st_mode & 0o111
        rules_sha, count = fetch.download_rules(
            base, "1.186.0", FAKE_TOKEN, tmp_path / "rules.json"
        )
        assert count == 3
        rules = json.loads((tmp_path / "rules.json").read_text())
        assert [r["id"] for r in rules["rules"]] == ["r1", "r2", "r3"], "sorted by id"
        assert list(rules) == ["rules"]
        calls = {path.split("?")[0]: query for path, query in FakeSemgrep.calls}
        assert all(auth == f"Bearer {FAKE_TOKEN}" for _, auth in FakeSemgrep.calls)
        assert any(
            "is_secrets_scan=true" in p and "semgrep_version=1.186.0" in p
            for p, _ in FakeSemgrep.calls
        )
        assert calls  # both endpoints were called
        # A wrong token ends with the status and without the token.
        with pytest.raises(SystemExit) as err:
            fetch.download_binary(
                base,
                "manylinux-x86",
                "1.186.0",
                "wrong-token-0123456789",
                tmp_path / "e2",
            )
        assert "HTTP 401" in str(err.value) and "wrong-token" not in str(err.value)
        fetch.open_url = original
        for path in tmp_path.iterdir():
            assert FAKE_TOKEN.encode() not in path.read_bytes(), (
                f"the token is in {path.name}"
            )
    finally:
        server.shutdown()


def test_fetch_stops_without_a_token_and_never_prints_it(tmp_path: Path) -> None:
    def run(token: str | None):
        env = {k: v for k, v in os.environ.items() if k != "SEMGREP_APP_TOKEN"}
        if token is not None:
            env["SEMGREP_APP_TOKEN"] = token
        return subprocess.run(
            [sys.executable, "-I", str(HERE / "fetch-pro.py"), str(tmp_path / "out")],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    assert run(None).returncode != 0 and "empty or not set" in run(None).stderr
    assert "empty or not set" in run("").stderr
    done = run("tok with spaces and more")
    assert done.returncode != 0 and "does not look like a token" in done.stderr
    assert "tok with spaces" not in done.stdout + done.stderr


def test_fetch_sends_the_token_to_https_only_and_drops_it_on_another_host() -> None:
    source = (HERE / "fetch-pro.py").read_text()
    assert 'startswith("https://")' in source
    assert "Bearer" in source and source.count("Bearer") == 1
    fetch = load(HERE / "fetch-pro.py", "fetch_pro2")
    import urllib.request

    handler = fetch.KeepAuthOnSameHost()
    req = urllib.request.Request("https://semgrep.dev/a")
    req.add_unredirected_header("Authorization", "Bearer secret-value-for-the-test")
    same = handler.redirect_request(
        req, None, 302, "Found", {}, "https://semgrep.dev/b"
    )
    other = handler.redirect_request(
        req, None, 302, "Found", {}, "https://cdn.example.com/c"
    )
    assert same.get_header("Authorization") == "Bearer secret-value-for-the-test"
    assert other.get_header("Authorization") is None


# --- the smoke client --------------------------------------------------------


def test_smoke_bodies_have_the_asked_size_and_the_secrets() -> None:
    smoke = load(HERE / "smoke-pro.py", "smoke_pro")
    for size in (2048, 16384, 524288):
        planted, clean = smoke.body_of(size, True), smoke.body_of(size, False)
        assert len(planted) == size and len(clean) == size
        assert planted != clean
        for prefix in (
            b"ghp_",
            b"AKIA",
            b"xoxb-",
            b"sk_live_",
            b"-----BEGIN RSA " + b"PRIVATE KEY-----",
        ):
            assert prefix in planted, prefix
            assert prefix not in clean, prefix
    assert smoke.fake_secrets() == smoke.fake_secrets(), "fixed seed"
    assert smoke.fake_secrets().count(b"\n") == 24, "ten lines and a 14-line key"


def test_a_failed_scan_prints_only_the_status_and_the_length() -> None:
    """The body of /scan is the engine's stdout: it can hold rule ids and matched text,
    and the job log of a public repository must not."""
    smoke = load(HERE / "smoke-pro.py", "smoke_pro_fail")
    secret_text = "rule.secrets.alpha-token matched ghp_SHOULD-NOT-APPEAR"

    class Handler(http.server.BaseHTTPRequestHandler):
        status = 500

        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b"." + secret_text.encode()
            self.send_response(Handler.status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Smoke:
        @staticmethod
        def fail(message: str) -> None:
            raise RuntimeError(message)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for status in (500, 200):
            Handler.status = status
            with pytest.raises(RuntimeError) as err:
                smoke.scan(Smoke, "127.0.0.1", server.server_port, b"{}")
            message = str(err.value)
            assert "answered" in message and "bytes" in message
            for text in ("alpha", "rule.", "ghp_", "SHOULD-NOT"):
                assert text not in message, (status, message)
    finally:
        server.shutdown()
    source = (HERE / "smoke-pro.py").read_text()
    assert "raw[:" not in source and "text!r" not in source


def test_the_smoke_client_prints_counts_and_never_a_rule_id() -> None:
    smoke = load(HERE / "smoke-pro.py", "smoke_pro_counts")

    class Fail(Exception):
        pass

    class Smoke:
        @staticmethod
        def fail(message: str) -> None:
            raise Fail(message)

    found = [
        {"check_id": "secret.rules.alpha-token"},
        {"check_id": "secret.rules.beta-key"},
    ]
    line = smoke.check_result(Smoke, {"results": found, "errors": []}, True, 1)
    assert line == "findings=2 rules=2"
    for text in ("alpha", "beta", "secret.rules", "ids="):
        assert text not in line
    with pytest.raises(Fail) as err:
        smoke.check_result(Smoke, {"results": found, "errors": []}, False, 1)
    assert "alpha" not in str(err.value) and "secret.rules" not in str(err.value)
    with pytest.raises(Fail) as err:
        smoke.check_result(
            Smoke,
            {"results": [], "errors": [{"message": "rule secret.rules.alpha failed"}]},
            True,
            1,
        )
    assert "alpha" not in str(err.value) and "errors=1" in str(err.value)
    source = (HERE / "smoke-pro.py").read_text()
    assert "ids=" not in source.replace("It never prints a rule id", "")


def test_no_source_file_holds_a_complete_fake_token() -> None:
    """A complete token in the repository would be flagged by secret scanners; the
    smoke client joins its parts at run time."""
    shapes = [
        r"ghp_[A-Za-z0-9]{36}",
        r"AKIA[0-9A-Z]{16}",
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        r"sk_live_[0-9a-zA-Z]{24}",
        r"-----BEGIN (RSA |EC |OPENSSH )?" + "PRIVATE KEY-----",
    ]
    for path in HERE.iterdir():
        if path.is_file() and path.suffix in {
            ".py",
            ".sh",
            ".c",
            ".md",
            ".env",
            ".json",
        }:
            text = path.read_text()
            for shape in shapes:
                assert not re.search(shape, text), f"{path.name} matches {shape}"


# --- the workflow ------------------------------------------------------------


def workflow_path() -> Path | None:
    for parent in HERE.parents:
        candidate = parent / ".github" / "workflows" / "substrate-fork-engine-pro.yml"
        if candidate.is_file():
            return candidate
    return None


@pytest.fixture
def workflow() -> tuple[str, dict]:
    path = workflow_path()
    if path is None:
        pytest.skip("the workflow file is outside this test's runfiles")
    text = path.read_text()
    return text, yaml.safe_load(text)


def step_named(steps: list[dict], prefix: str) -> dict:
    (found,) = [s for s in steps if s.get("name", "").startswith(prefix)]
    return found


def test_workflow_is_manual_one_repository_and_one_image_name(workflow) -> None:
    text, wf = workflow
    assert list(wf[True]) == ["workflow_dispatch"], "no push or pull_request trigger"
    assert (
        wf["env"]["IMAGE_REPOSITORY"]
        == "ghcr.io/jomcgi-org/homelab/substrate-fork/engine-pro"
    )
    # Read-only at the top. The one job has the one write scope it needs: the
    # publish steps (canary, image, checks) are in it. No other job exists.
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["image"]
    assert job["if"] == "github.repository == 'jomcgi-org/homelab'"
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert job["environment"] == "semgrep-pro-build"
    assert len(wf["jobs"]) == 1
    # No artifact, no cache, no other registry, no other action than checkout.
    for banned in (
        "upload-artifact",
        "actions/cache",
        "download-artifact",
        "docker push",
        "docker login",
    ):
        assert banned not in text, banned
    checkouts = [s for s in job["steps"] if "uses" in s]
    assert len(checkouts) == 1
    assert checkouts[0]["with"] == {"persist-credentials": False}, (
        "the checkout must not leave the token in .git/config"
    )
    for step in job["steps"]:
        if "uses" in step:
            assert step["uses"].startswith("actions/checkout@")
            assert re.search(r"@[0-9a-f]{40}( #.*)?$", step["uses"]), step["uses"]
    assert text.count("build.sh push") == 1


def test_no_expression_expands_inside_a_script(workflow) -> None:
    _, wf = workflow
    for step in wf["jobs"]["image"]["steps"]:
        assert "${{" not in step.get("run", ""), step["name"]
        # An expression in "if" or "env" or "with" is fine; "run" never has one.


def test_the_token_is_in_the_download_and_the_scan_steps_only(workflow) -> None:
    text, wf = workflow
    steps = wf["jobs"]["image"]["steps"]
    holders = [s["name"] for s in steps if "SEMGREP_APP_TOKEN" in str(s.get("env", {}))]
    assert holders == [
        "Download the Pro engine and the rules",
        "Scan the layer and the image for the token",
    ]
    for step in steps:
        if step.get("name") in holders:
            assert step["env"] == {
                "SEMGREP_APP_TOKEN": "${{ secrets.SEMGREP_APP_TOKEN }}"
            }
        else:
            assert "secrets." not in str(step), step["name"]
            assert "SEMGREP_APP_TOKEN" not in step.get("run", ""), step["name"]
    # No job-level or workflow-level env holds it, and nothing writes it to a file or GITHUB_ENV.
    assert (
        "SEMGREP_APP_TOKEN" not in str(wf["env"]) and "env" not in wf["jobs"]["image"]
    )
    assert text.count("secrets.SEMGREP_APP_TOKEN") == 2
    assert "GITHUB_ENV" not in text and "GITHUB_OUTPUT" not in text
    download = step_named(steps, "Download")
    assert download["run"].startswith("set +x\n")
    assert "::add-mask::${SEMGREP_APP_TOKEN}" in download["run"]
    assert "fetch-pro.py" in download["run"]
    for banned in (
        'echo "${SEMGREP_APP_TOKEN}"',
        "printenv",
        "env |",
        "set -x",
        "curl",
        "tee",
    ):
        assert banned not in download["run"], banned
    scan = step_named(steps, "Scan the layer")
    assert scan["run"].startswith("set +x\n") and "token-scan.py" in scan["run"]
    assert "curl" not in scan["run"] and "docker" not in scan["run"]


def test_the_layer_and_the_test_steps_run_after_the_download_without_the_token(
    workflow,
) -> None:
    _, wf = workflow
    steps = wf["jobs"]["image"]["steps"]
    names = [s["name"] for s in steps if "name" in s]
    order = [
        "Validate inputs",
        "Install crane",
        "Download the Pro engine and the rules",
        "Build the layer",
        "Build the image",
        "Test the image with the placeholder token",
        "Scan the layer and the image for the token",
        "Refuse to publish from a branch other than main",
        "Log in to ghcr.io",
        "Publish the canary",
        "Check that the canary is private",
        "Publish the image",
        "Check that an anonymous pull is refused",
    ]
    assert [n for n in names if n in order] == order
    test = step_named(steps, "Test the image")
    assert "--internal" in test["run"], "a network with no route out"
    assert "--read-only" in test["run"] and "--cap-drop ALL" in test["run"]
    for mode in (
        "planted 16384",
        "planted 524288",
        "clean 16384",
        "check engine-edge edge",
    ):
        assert mode in test["run"], mode
    assert "smoke-pro.py" in test["run"] and "FORKSRV_CHECKPOINT=0" in test["run"]
    assert "engine-pro:local" in test["run"]


def test_publish_runs_only_on_main_and_never_overwrites(workflow) -> None:
    text, wf = workflow
    steps = wf["jobs"]["image"]["steps"]
    main_only = "inputs.publish && github.ref == 'refs/heads/main'"
    for step in steps:
        run = step.get("run", "")
        if re.search(r"crane auth login|build\.sh push|crane push|canary\.sh", run):
            assert step["if"] == main_only, step["name"]
    assert (
        step_named(steps, "Refuse to publish")["if"]
        == "inputs.publish && github.ref != 'refs/heads/main'"
    )
    # The input is checked before the download, and publish from a branch fails there too.
    validate = step_named(steps, "Validate inputs")
    assert validate["env"] == {
        "PUBLISH": "${{ inputs.publish }}",
        "DEBUG": "${{ inputs.debug }}",
        "REF": "${{ github.ref }}",
    }
    assert 'REF}" != "refs/heads/main"' in validate["run"]
    publish = step_named(steps, "Publish the image")
    assert "crane digest" in publish["run"]
    assert (
        "MANIFEST_UNKNOWN|NAME_UNKNOWN" in publish["run"]
        and "Cannot tell whether" in publish["run"]
    )
    assert "${SEMGREP_VERSION}-${COMMIT:0:12}" in publish["run"]


def test_a_content_free_canary_must_pass_the_check_before_the_pro_image(
    workflow,
) -> None:
    _, wf = workflow
    steps = wf["jobs"]["image"]["steps"]
    names = [s.get("name") for s in steps]
    canary = step_named(steps, "Publish the canary")
    check = step_named(steps, "Check that the canary is private")
    publish = step_named(steps, "Publish the image")
    main_only = "inputs.publish && github.ref == 'refs/heads/main'"
    assert canary["if"] == main_only and check["if"] == main_only
    assert names.index("Publish the canary") < names.index(
        "Check that the canary is private"
    )
    assert names.index("Check that the canary is private") < names.index(
        "Publish the image"
    )
    # No "always()" on the canary check or the Pro publish: a failed check skips the push.
    assert "always()" not in check["if"] and "always()" not in publish["if"]
    assert "./canary.sh" in canary["run"] and "canary-" in canary["run"]
    assert "./check-private.sh --no-delete" in check["run"]
    # The canary and the Pro image go to the same package name.
    assert "${IMAGE_REPOSITORY}:canary-" in canary["run"]
    assert "build.sh" not in canary["run"]


def test_the_engine_log_is_printed_only_with_the_debug_input(workflow) -> None:
    """The job log of a public repository must not carry a rule id or a finding text."""
    text, wf = workflow
    dispatch = wf[True]["workflow_dispatch"]["inputs"]
    assert dispatch["debug"]["type"] == "boolean"
    assert dispatch["debug"]["default"] is False
    steps = wf["jobs"]["image"]["steps"]
    test = step_named(steps, "Test the image")
    assert test["env"]["DEBUG"] == "${{ inputs.debug }}"
    run = test["run"]
    assert run.count("docker logs") == 1
    before = run[: run.index("docker logs")].rstrip().splitlines()
    assert before[-1].strip() == 'if [[ "${DEBUG}" == "true" ]]; then'
    # A debug run cannot publish.
    validate = step_named(steps, "Validate inputs")
    assert '"${DEBUG}" == "true" && "${PUBLISH}" == "true"' in validate["run"]
    # Nothing else prints an engine answer or a log.
    for step in steps:
        if step is not test:
            assert "docker logs" not in step.get("run", ""), step.get("name")


def test_nothing_can_swallow_a_failed_check(workflow) -> None:
    """Three mutations that a review found undetected: continue-on-error, an `||` after
    the canary check, and a canary check that tests another reference than the one pushed."""
    text, wf = workflow
    assert "continue-on-error" not in text
    steps = wf["jobs"]["image"]["steps"]
    assert "continue-on-error" not in wf["jobs"]["image"]
    for step in steps:
        assert "continue-on-error" not in step, step.get("name")
    push = step_named(steps, "Publish the canary")
    check = step_named(steps, "Check that the canary is private")
    final = step_named(steps, "Check that an anonymous pull is refused")
    for step in (push, check, final, step_named(steps, "Publish the image")):
        for line in step["run"].splitlines():
            code = line.split("#", 1)[0]
            assert "||" not in code, (step["name"], line)
            assert not code.rstrip().endswith("&") and "set +e" not in code, line
    assert "set -euo pipefail" in check["run"] and "set -euo pipefail" in push["run"]

    def ref_arg(run: str, script: str) -> str:
        (arg,) = re.findall(rf'{re.escape(script)}(?: --no-delete)? ("[^"]+")', run)
        return arg

    pushed = ref_arg(push["run"], "./canary.sh")
    checked = ref_arg(check["run"], "./check-private.sh")
    assert pushed == checked, (
        "the canary check must test the reference that the canary push made"
    )
    assert pushed == '"${IMAGE_REPOSITORY}:canary-${SEMGREP_VERSION}-${COMMIT:0:12}"'
    # Both steps read the same COMMIT and the same pins.
    assert push["env"] == check["env"] == {"COMMIT": "${{ github.sha }}"}
    assert (
        ". ../engine/pins.env" in push["run"] and ". ../engine/pins.env" in check["run"]
    )


def test_the_anonymous_pull_must_be_refused_after_the_push(workflow) -> None:
    _, wf = workflow
    steps = wf["jobs"]["image"]["steps"]
    names = [s.get("name") for s in steps]
    guard = step_named(steps, "Check that an anonymous pull is refused")
    assert names.index(guard["name"]) > names.index("Publish the image")
    assert "check-private.sh" in guard["run"]
    assert guard["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "always()" in guard["if"] and "inputs.publish" in guard["if"]
    assert "steps.publish.outcome != 'skipped'" in guard["if"], (
        "runs after a failed publish that pushed"
    )
    assert "SEMGREP_APP_TOKEN" not in str(guard)
    # A digest that does not resolve fails the job (no "if digest=..." around it).
    assert 'digest="$(crane digest "${ref}")"' in guard["run"]
    assert "if digest=" not in guard["run"] and "2>/dev/null" not in guard["run"]
    script = (HERE / "check-private.sh").read_text()
    assert "env -i" in script and "DOCKER_CONFIG" in script
    # The authenticated resolve comes before the anonymous call.
    assert script.index("# Step 1") < script.index("# Step 2")
    assert script.index('crane manifest "$ref"') < script.index("env -i PATH")
    assert "--method DELETE" in script and "exit 1" in script


def test_the_image_has_no_source_label_that_links_it_to_the_public_repository() -> None:
    build = (HERE / "build.sh").read_text()
    assert "image.source" not in build.replace(
        "no org.opencontainers.image.source label", ""
    )
    assert "LicenseRef-Semgrep-Proprietary" in build and "--user 65532:65532" in build
    assert "SEMGREP_APP_TOKEN" in build and "exit 1" in build
