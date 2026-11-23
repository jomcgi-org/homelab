"""Run node-disk-gc.sh against a fake home and a fake temporary directory."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("node-disk-gc.sh").resolve()
GO_README = "This directory holds cached build artifacts from the Go build system.\n"
DAY = 24 * 3600


def age(path, seconds):
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def write(path, size=100, old=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if old is not None:
        age(path, old)
    return path


def old_tree(path, hours=48):
    """A directory of read-only files, like a test Postgres data directory."""
    inner = write(path / "base" / "1" / "relfile", old=hours * 3600)
    inner.chmod(0o400)
    age(path / "base" / "1", hours * 3600)
    age(path / "base", hours * 3600)
    age(path, hours * 3600)
    return path


@pytest.fixture
def node(tmp_path):
    home = tmp_path / "home"
    go = home / ".cache" / "go-build"
    bazel = home / ".cache" / "bazel" / "disk_cache"
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    write(go / "README", old=30 * DAY).write_text(GO_README)
    write(go / "trim.txt", old=30 * DAY)
    write(go / "ab" / "old-a", 1000, old=10 * DAY)
    write(go / "ab" / "new-a", 1000, old=1 * DAY)
    write(bazel / "ac" / "ab" / "old", 2000, old=20 * DAY)
    write(bazel / "cas" / "cd" / "old", 2000, old=20 * DAY)
    write(bazel / "cas" / "cd" / "new", 2000, old=1 * DAY)
    write(bazel / "gc" / "lock", 10, old=20 * DAY)
    write(bazel / "other" / "old", 10, old=20 * DAY)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GOCACHE": str(go),
        "BAZEL_DISK_CACHE": str(bazel),
        "GC_TMP_DIR": str(tmp),
    }
    return {"home": home, "go": go, "bazel": bazel, "tmp": tmp, "env": env}


def run(node, *args, env=None):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env or node["env"],
        capture_output=True,
        text=True,
        check=False,
    )


def files(root):
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_dry_run_changes_nothing(node):
    old_tree(node["tmp"] / "pgdata123")
    before = [files(node[k]) for k in ("go", "bazel", "tmp")]
    result = run(node)
    assert result.returncode == 0, result.stderr
    assert [files(node[k]) for k in ("go", "bazel", "tmp")] == before
    out = result.stdout
    assert "node-disk-gc: dry-run" in out
    assert "Go build cache" in out and "1 files" in out
    assert "Test directories" in out and "1," in out
    assert "Dry run. Run with --apply" in out
    assert "root filesystem before:" in out


def test_apply_removes_only_old_cache_entries(node):
    result = run(node, "--apply")
    assert result.returncode == 0, result.stderr
    assert files(node["go"]) == {"README", "trim.txt", "ab/new-a"}
    assert files(node["bazel"]) == {"cas/cd/new", "gc/lock", "other/old"}
    assert "root filesystem after:" in result.stdout
    assert "Removed:" in result.stdout


def test_apply_removes_old_test_directories(node):
    tmp = node["tmp"]
    old_tree(tmp / "pgdata-a")
    old_tree(tmp / "tmpabc")
    old_tree(tmp / "other-dir")  # not a test directory name
    old_tree(tmp / "pgdata-new", hours=2)  # newer than 12 h
    fresh = old_tree(tmp / "pgdata-fresh-file")
    write(fresh / "base" / "new-file")  # an old directory with a new file
    (tmp / "target").mkdir()
    write(tmp / "target" / "keep", old=48 * 3600)
    (tmp / "tmplink").symlink_to(tmp / "target")
    age(tmp / "tmplink", 48 * 3600)
    result = run(node, "--apply", "--tmp-hours", "12")
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in tmp.iterdir()) == [
        "other-dir",
        "pgdata-fresh-file",
        "pgdata-new",
        "target",
        "tmplink",
    ]
    assert (tmp / "target" / "keep").exists()


def test_directories_in_use_stay(node):
    tmp = node["tmp"]
    cwd_dir = old_tree(tmp / "pgdata-cwd")
    arg_dir = old_tree(tmp / "pgdata-arg")
    open_dir = old_tree(tmp / "pgdata-open")
    gone = old_tree(tmp / "pgdata-gone")
    sleeper = "import time; time.sleep(60)"
    holders = [
        subprocess.Popen([sys.executable, "-c", sleeper], cwd=cwd_dir),
        subprocess.Popen([sys.executable, "-c", sleeper, str(arg_dir)]),
    ]
    try:
        with open(open_dir / "base" / "1" / "relfile", "rb"):
            time.sleep(0.3)
            result = run(node, "--apply")
    finally:
        for holder in holders:
            holder.kill()
            holder.wait()
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in tmp.iterdir()) == [
        "pgdata-arg",
        "pgdata-cwd",
        "pgdata-open",
    ]
    assert not gone.exists()
    assert "keep (in use)" in result.stdout


def test_a_directory_that_is_not_a_cache_is_refused(node, tmp_path):
    # No Go README marker: skipped. A path that is not named disk_cache: skipped.
    elsewhere = tmp_path / "home" / "documents"
    write(elsewhere / "old", old=90 * DAY)
    (node["go"] / "README").write_text("not a Go cache\n")
    env = dict(node["env"], GOCACHE=str(node["go"]), BAZEL_DISK_CACHE=str(elsewhere))
    before = files(node["go"]), files(elsewhere)
    result = run(node, "--apply", env=env)
    assert result.returncode == 0, result.stderr
    assert (files(node["go"]), files(elsewhere)) == before
    assert "skipped (not a Go build cache)" in result.stdout
    assert "skipped (not a disk_cache directory)" in result.stdout


def test_age_options(node):
    result = run(node, "--apply", "--go-days", "0", "--bazel-days", "30")
    assert result.returncode == 0, result.stderr
    assert files(node["go"]) == {"README", "trim.txt"}
    assert "cas/cd/old" in files(node["bazel"])


@pytest.mark.parametrize(
    "args",
    [["--delete"], ["--go-days"], ["--go-days", "x"], ["--tmp-hours", "-1"], ["extra"]],
)
def test_bad_arguments_exit_2(node, args):
    result = run(node, *args)
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert "Go build cache" not in result.stdout
