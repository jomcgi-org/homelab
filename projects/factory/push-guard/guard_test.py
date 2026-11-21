import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", repo, *args], check=check, capture_output=True)


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    # Git executes the hook by its python3 shebang, outside the pytest launcher.
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(tools), prepend=os.pathsep)
    remote = tmp_path / "private.git"
    git(tmp_path, "init", "--bare", "--template=", remote)
    hook = remote / "hooks/pre-receive"
    hook.parent.mkdir()
    git(remote, "config", "core.hooksPath", str(hook.parent))
    shutil.copy(Path(__file__).with_name("pre-receive"), hook)
    refs = [tmp_path / name for name in ("binary", "layer")]
    for path in refs:
        path.write_bytes(os.urandom(65536))
    index = tmp_path / "index"
    with index.open("wb") as out:
        subprocess.run([hook, "--index", *refs], check=True, stdout=out)
    monkeypatch.setenv("MANDOLINE_INDEX", str(index))
    upstream = tmp_path / "public-simulation.git"
    git(tmp_path, "init", "--bare", upstream)
    # A local transport inherits quarantine variables that an HTTPS server
    # never sees. Clear them only in the simulated public receive-pack.
    git(remote, "remote", "add", "public-fixture", str(upstream))
    git(
        remote,
        "config",
        "remote.public-fixture.receivepack",
        "env -u GIT_QUARANTINE_PATH -u GIT_OBJECT_DIRECTORY "
        "-u GIT_ALTERNATE_OBJECT_DIRECTORIES git-receive-pack",
    )
    monkeypatch.setenv("PUSH_UPSTREAM", "public-fixture")
    repo = tmp_path / "work"
    git(tmp_path, "init", repo)
    git(repo, "config", "user.name", "fixture")
    git(repo, "config", "user.email", "fixture@example.org")
    return repo, remote, refs, upstream


@pytest.mark.parametrize(
    "sample",
    [
        "ordinary",
        "large",
        "binary",
        "layer",
        "base64",
        "hex",
        "allowed",
        "allowed-protected",
        "missing-index",
    ],
)
def test_push(receiver, sample, monkeypatch):
    repo, remote, refs, upstream = receiver
    part = refs[sample == "layer"].read_bytes()[1001:24003]
    data = {
        "ordinary": b"ordinary source change\n",
        "large": bytes(1048577),
        "base64": base64.encodebytes(part),
        "hex": part.hex().encode(),
    }.get(sample, part)
    if sample.startswith("allowed"):
        data = b"x" * 1048577 + (part if sample == "allowed-protected" else b"")
        allowlist = repo.parent / "allowlist"
        allowlist.write_text(hashlib.sha256(data).hexdigest())
        monkeypatch.setenv("PUSH_SIZE_ALLOWLIST", str(allowlist))
    if sample == "missing-index":
        monkeypatch.delenv("MANDOLINE_INDEX")
    path = repo / "fixture"
    path.write_bytes(data)
    git(repo, "add", "fixture")
    git(repo, "commit", "-m", "fixture")
    path.unlink()
    git(repo, "commit", "-am", "remove fixture at tip")
    result = git(repo, "push", remote, "HEAD:refs/heads/factory/check", check=False)
    published = git(
        upstream, "show-ref", "--verify", "refs/heads/factory/check", check=False
    )
    if sample in ("ordinary", "allowed"):
        assert result.returncode == published.returncode == 0, result.stderr.decode()
    else:
        assert b"push rejected" in result.stderr
        assert result.returncode and published.returncode
