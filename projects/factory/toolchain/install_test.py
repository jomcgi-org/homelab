"""Download integrity and relocation checks, with no network or credentials."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
from unittest.mock import patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sandbox_install", Path(__file__).with_name("install.py")
)
_INSTALL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_INSTALL)


@pytest.mark.parametrize("archive", [False, True])
def test_verified_executable_runs_after_relocation(tmp_path, archive):
    source = b'#!/bin/sh\nprintf "verified:%s" "$1"\n'
    payload = source
    pin = {"kind": "file", "url": "https://github.com/example/tool"}
    if archive:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
            member = tarfile.TarInfo("release/tool")
            member.size = len(source)
            bundle.addfile(member, io.BytesIO(source))
            # A downloader must ignore unrelated archive entries.
            member = tarfile.TarInfo("../../escape")
            member.size = len(source)
            bundle.addfile(member, io.BytesIO(source))
        payload = stream.getvalue()
        pin.update(kind="archive", file="release/tool")
    pin["sha256"] = hashlib.sha256(payload).hexdigest()
    root = tmp_path / "staging"
    (root / "bin").mkdir(parents=True)
    with patch.object(
        _INSTALL.urllib.request, "urlopen", return_value=io.BytesIO(payload)
    ):
        _INSTALL.install_binary("tool", pin, root)
    _INSTALL.wrapper(root, "wrapped", '"$root/bin/tool"')
    destination = tmp_path / "cache with spaces"
    root.rename(destination)
    assert (
        subprocess.check_output([destination / "bin/wrapped", "ok"]) == b"verified:ok"
    )
    assert not (tmp_path / "escape").exists()


def test_checksum_failure_never_installs_executable(tmp_path):
    (tmp_path / "bin").mkdir()
    pin = {
        "kind": "file",
        "url": "https://github.com/example/tool",
        "sha256": "0" * 64,
    }
    with patch.object(
        _INSTALL.urllib.request, "urlopen", return_value=io.BytesIO(b"bad")
    ):
        with pytest.raises(ValueError, match="checksum mismatch"):
            _INSTALL.install_binary("tool", pin, tmp_path)
    assert not (tmp_path / "bin/tool").exists()


def test_archive_symlink_is_rejected(tmp_path):
    (tmp_path / "bin").mkdir()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
        member = tarfile.TarInfo("tool")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        bundle.addfile(member)
    payload = stream.getvalue()
    pin = {
        "kind": "archive",
        "file": "tool",
        "url": "https://github.com/example/tool",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    with patch.object(
        _INSTALL.urllib.request, "urlopen", return_value=io.BytesIO(payload)
    ):
        with pytest.raises(ValueError, match="not a regular"):
            _INSTALL.install_binary("tool", pin, tmp_path)
    assert not (tmp_path / "bin/tool").exists()


def test_runtime_packages_follow_repository_pins_without_install_scripts(tmp_path):
    repo = tmp_path / "repo"
    (repo / "bazel/tools").mkdir(parents=True)
    root = tmp_path / "tools"
    (root / "bin").mkdir(parents=True)
    tools = ("helm", "crane", "buildifier", "gofumpt", "shfmt", "shellcheck")
    locks = {
        name: {"binaries": [{"os": "linux", "cpu": "x86_64", "name": name}]}
        for name in tools
    }
    locks["ruff"] = {
        "binaries": [
            {
                "url": "https://github.com/astral-sh/ruff/releases/download/0.15.5/ruff.tar.gz"
            }
        ]
    }
    (repo / "bazel/tools/tools.lock.json").write_text(json.dumps(locks))
    (repo / "package.json").write_text(
        json.dumps(
            {
                "packageManager": "pnpm@10.34.5",
                "devDependencies": {
                    "prettier": "3.9.6",
                    "prettier-plugin-svelte": "4.1.1",
                    "svelte": "5.56.9",
                },
            }
        )
    )
    with (
        patch.object(_INSTALL, "install_binary") as binary,
        patch.object(_INSTALL.subprocess, "run") as run,
    ):
        _INSTALL.install(repo, root)
    assert [call.args[0] for call in binary.call_args_list] == list(tools)
    pip, npm = [call.args[0] for call in run.call_args_list]
    assert "ruff==0.15.5" in pip
    assert "--only-binary=:all:" in pip and "--no-deps" in pip
    assert npm[-4:] == [
        "pnpm@10.34.5",
        "prettier@3.9.6",
        "prettier-plugin-svelte@4.1.1",
        "svelte@5.56.9",
    ]
    assert "--ignore-scripts" in npm
    assert all(call.kwargs["check"] for call in run.call_args_list)
