"""Run node-disk-gc.sh against a fake home and a fake temporary directory."""

import os
import shutil
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
        # The test runner can be root. The script refuses root without this.
        "GC_ALLOW_ROOT": "1",
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
    [
        ["--delete"],
        ["--go-days"],
        ["--go-days", "x"],
        ["--tmp-hours", "-1"],
        ["--tmp-hours", "0"],
        ["--tmp-hours", "08"],
        ["extra"],
    ],
)
def test_bad_arguments_exit_2(node, args):
    result = run(node, *args)
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert "Go build cache" not in result.stdout


SLEEPER = "import time; time.sleep(60)"


def stop(*processes):
    for process in processes:
        process.kill()
        process.wait()


def remaining(tmp):
    return sorted(p.name for p in tmp.iterdir())


def test_a_mount_point_under_a_directory_keeps_it(node, tmp_path):
    tmp = node["tmp"]
    mounted = old_tree(tmp / "pgdata-mounted")
    exact = old_tree(tmp / "tmp-exact")
    # Octal escapes: a mount point named "/.../pgdata-space/a b".
    spaced = old_tree(tmp / "pgdata-space")
    gone = old_tree(tmp / "pgdata-gone")
    table = tmp_path / "mountinfo"
    table.write_text(
        f"1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        f"2 1 0:30 / {mounted}/base/1 rw - tmpfs tmpfs rw\n"
        f"3 1 0:31 / {exact} rw - tmpfs tmpfs rw\n"
        f"4 1 0:32 / {spaced}/a\\040b rw - tmpfs tmpfs rw\n"
        f"5 1 0:33 / {tmp}/pgdata-goneX rw - tmpfs tmpfs rw\n"
    )
    env = dict(node["env"], GC_MOUNTINFO=str(table))
    result = run(node, "--apply", env=env)
    assert result.returncode == 0, result.stderr
    assert remaining(tmp) == ["pgdata-mounted", "pgdata-space", "tmp-exact"]
    assert result.stdout.count("keep (mount)") == 3
    assert not gone.exists()


def test_a_unix_socket_path_keeps_the_directory(node):
    tmp = node["tmp"]
    held = old_tree(tmp / "pgdata-sock")
    gone = old_tree(tmp / "pgdata-gone")
    code = (
        "import socket, sys, time\n"
        "s = socket.socket(socket.AF_UNIX)\n"
        "s.bind(sys.stdin.readline().strip())\n"
        "print('bound', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        holder.stdin.write(f"{held}/.s\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "bound"
        # The new socket file and directory time would keep the directory.
        age(held / ".s", 48 * 3600)
        age(held, 48 * 3600)
        result = run(node, "--apply")
    finally:
        stop(holder)
    assert result.returncode == 0, result.stderr
    assert remaining(tmp) == ["pgdata-sock"]
    assert not gone.exists()


def test_a_binary_run_from_the_directory_keeps_it(node):
    tmp = node["tmp"]
    held = old_tree(tmp / "pgdata-exe")
    binary = held / "sleep"
    shutil.copy2(shutil.which("sleep"), binary)
    age(binary, 48 * 3600)
    age(held, 48 * 3600)
    gone = old_tree(tmp / "pgdata-gone")
    # argv[0] is "sleep", so only the executable and its mappings name the path.
    holder = subprocess.Popen(["sleep", "60"], executable=str(binary))
    try:
        time.sleep(0.3)
        result = run(node, "--apply")
    finally:
        stop(holder)
    assert result.returncode == 0, result.stderr
    assert remaining(tmp) == ["pgdata-exe"]
    assert not gone.exists()


def test_a_mapped_library_keeps_the_directory(node):
    tmp = node["tmp"]
    held = old_tree(tmp / "pgdata-maps")
    library = held / "libm-copy.so"
    libm = next(Path("/lib").rglob("libm.so.6"), None)
    if libm is None:
        pytest.skip("no libm.so.6 on this host")
    shutil.copy2(libm.resolve(), library)
    age(library, 48 * 3600)
    age(held, 48 * 3600)
    gone = old_tree(tmp / "pgdata-gone")
    # The path comes through stdin, so no argument, variable or open file has it.
    code = (
        "import ctypes, sys, time\n"
        "ctypes.CDLL(sys.stdin.readline().strip())\n"
        "print('loaded', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        holder.stdin.write(f"{library}\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "loaded"
        result = run(node, "--apply")
    finally:
        stop(holder)
    assert result.returncode == 0, result.stderr
    assert remaining(tmp) == ["pgdata-maps"]
    assert not gone.exists()


def test_an_environment_value_keeps_the_directory(node):
    tmp = node["tmp"]
    held = old_tree(tmp / "pgdata-env")
    gone = old_tree(tmp / "pgdata-gone")
    env = {"PATH": os.environ["PATH"], "TMPDIR": str(held)}
    holder = subprocess.Popen([sys.executable, "-c", SLEEPER], env=env)
    try:
        time.sleep(0.3)
        result = run(node, "--apply")
    finally:
        stop(holder)
    assert result.returncode == 0, result.stderr
    assert remaining(tmp) == ["pgdata-env"]
    assert not gone.exists()


def test_an_unreadable_subdirectory_does_not_stop_the_run(node):
    tmp = node["tmp"]
    locked = old_tree(tmp / "pgdata-locked")
    inner = locked / "private"
    write(inner / "file", old=48 * 3600)
    age(inner, 48 * 3600)
    age(locked, 48 * 3600)
    inner.chmod(0)
    other = old_tree(tmp / "pgdata-other")
    try:
        result = run(node, "--apply")
    finally:
        if inner.exists():
            inner.chmod(0o700)
    assert result.returncode == 0, result.stderr
    assert not other.exists()
    assert not locked.exists()
    assert "failed:" not in result.stdout


def test_a_failed_removal_is_reported_and_the_run_goes_on(node, tmp_path):
    tmp = node["tmp"]
    stuck = old_tree(tmp / "pgdata-stuck")
    other = old_tree(tmp / "pgdata-other")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "rm").write_text(
        '#!/bin/sh\ncase "$*" in *pgdata-stuck*) exit 1 ;; esac\nexec /bin/rm "$@"\n'
    )
    (fake / "rm").chmod(0o755)
    env = dict(node["env"], PATH=f"{fake}:{os.environ['PATH']}")
    result = run(node, "--apply", env=env)
    assert result.returncode == 0, result.stderr
    assert f"failed: {stuck}" in result.stdout
    assert stuck.exists() and not other.exists()
    # Only the removed directory counts: 100 B in one file, not 200 B.
    assert "Removed:" in result.stdout


def test_a_newline_in_a_name_keeps_the_directory(node):
    tmp = node["tmp"]
    odd = old_tree(tmp / "pgdata-a\nb")
    other = old_tree(tmp / "pgdata-other")
    result = run(node, "--apply")
    assert result.returncode == 0, result.stderr
    assert odd.exists() and not other.exists()
    assert "keep (name has a newline)" in result.stdout


def test_symlinks_in_a_cache_are_not_followed(node, tmp_path):
    outside = tmp_path / "outside"
    keep = write(outside / "precious", old=90 * DAY)
    (node["go"] / "ab" / "filelink").symlink_to(keep)
    (node["go"] / "ab" / "dirlink").symlink_to(outside, target_is_directory=True)
    (node["bazel"] / "cas" / "cd" / "dirlink").symlink_to(
        outside, target_is_directory=True
    )
    for link in (
        node["go"] / "ab" / "filelink",
        node["go"] / "ab" / "dirlink",
        node["bazel"] / "cas" / "cd" / "dirlink",
    ):
        age(link, 90 * DAY)
    result = run(node, "--apply")
    assert result.returncode == 0, result.stderr
    assert keep.exists()
    assert (node["go"] / "ab" / "dirlink").is_symlink()
    assert (node["go"] / "ab" / "filelink").is_symlink()


def test_the_go_fuzz_corpus_stays(node):
    write(node["go"] / "fuzz" / "pkg" / "Fuzz" / "seed", old=30 * DAY)
    result = run(node, "--apply")
    assert result.returncode == 0, result.stderr
    assert "fuzz/pkg/Fuzz/seed" in files(node["go"])


def test_ages_are_exact(node):
    write(node["go"] / "ab" / "two-and-a-half", old=int(2.5 * DAY))
    write(node["go"] / "ab" / "one-and-a-half", old=int(1.5 * DAY))
    result = run(node, "--apply", "--go-days", "2")
    assert result.returncode == 0, result.stderr
    names = files(node["go"])
    assert "ab/two-and-a-half" not in names
    assert "ab/one-and-a-half" in names


@pytest.mark.parametrize("value", ["/", "relative", "{tmp}/", "{home}"])
def test_unsafe_tmp_directories_are_refused(node, value):
    env = dict(
        node["env"],
        GC_TMP_DIR=value.format(tmp=node["tmp"], home=node["home"]),
    )
    result = run(node, "--apply", env=dict(env, HOME=str(node["home"])))
    assert result.returncode == 2
    assert "GC_TMP_DIR" in result.stderr


@pytest.mark.skipif(os.geteuid() != 0, reason="the refusal is for root")
def test_root_is_refused(node):
    env = {k: v for k, v in node["env"].items() if k != "GC_ALLOW_ROOT"}
    result = run(node, "--apply", env=env)
    assert result.returncode == 2
    assert "not as root" in result.stderr
