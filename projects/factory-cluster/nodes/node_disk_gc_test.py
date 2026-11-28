"""Run node-disk-gc.sh against a fake home and a fake temporary directory."""

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("node-disk-gc.sh").resolve()
GO_README = "This directory holds cached build artifacts from the Go build system.\n"
DAY = 24 * 3600
PR_SET_DUMPABLE = 4


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
def base(tmp_path_factory):
    """A directory for the fixture. When the runner is root, the test process
    gives up root for good, because the script refuses to run as root and
    reads /proc only for processes of its own user: the real, effective and
    saved user ids must all match."""
    if os.geteuid() != 0:
        yield tmp_path_factory.mktemp("gc")
        return
    path = Path(tempfile.mkdtemp(prefix="gc", dir="/tmp"))
    path.chmod(0o755)
    os.chown(path, 65534, 65534)
    os.setgroups([])
    os.setresgid(65534, 65534, 65534)
    os.setresuid(65534, 65534, 65534)
    # A process that changed its user is not dumpable, so its /proc entries
    # would be closed to the script. Make it dumpable again.
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 1, 0, 0, 0)
    try:
        yield path
    finally:
        subprocess.run(["chmod", "-R", "u+rwX", str(path)], check=False)
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def node(base):
    tmp_path = base
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


def test_a_directory_that_is_not_a_cache_is_refused(node, base):
    # No Go README marker: skipped. A path that is not named disk_cache: skipped.
    elsewhere = base / "home" / "documents"
    write(elsewhere / "old", old=90 * DAY)
    (node["go"] / "README").write_text("not a Go cache\n")
    env = dict(node["env"], GOCACHE=str(node["go"]), BAZEL_DISK_CACHE=str(elsewhere))
    before = files(node["go"]), files(elsewhere)
    result = run(node, "--apply", env=env)
    assert result.returncode == 0, result.stderr
    assert (files(node["go"]), files(elsewhere)) == before
    assert "skipped (not a Go build cache" in result.stdout
    assert "skipped (not a disk_cache directory" in result.stdout


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
        ["--tmp-hours", "1000000"],
        ["--go-days", "99999999999999999999"],
        ["--bazel-days", "1000000"],
        ["--scratch-days", "0"],
        ["--go-max-gib", "x"],
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


def namespaces_work():
    probe = subprocess.run(
        ["unshare", "-Urm", "true"], capture_output=True, check=False
    )
    return probe.returncode == 0


needs_unshare = pytest.mark.skipif(
    shutil.which("unshare") is None or not namespaces_work(),
    reason="no user and mount namespaces on this host",
)


def run_with_mounts(node, mounts, *args, env=None):
    """Run the script as a normal user in a mount namespace with bind mounts.

    The outer namespace maps the caller to root, so it can mount. The script
    then runs in a nested user namespace as uid 1000, so it is not root.
    """
    command = (
        'while [ "$1" != -- ]; do mount --bind "$1" "$2" || exit 99; shift 2; done;'
        ' shift; exec unshare -U --map-user=1000 --map-group=1000 bash "$@"'
    )
    flat = [str(p) for pair in mounts for p in pair]
    return subprocess.run(
        [
            "unshare",
            "-Urm",
            "bash",
            "-c",
            command,
            "_",
            *flat,
            "--",
            str(SCRIPT),
            *args,
        ],
        env=env or node["env"],
        capture_output=True,
        text=True,
        check=False,
    )


def mount_fixture(tmp):
    """Directories with a bind mount in them, and one that is a mount point."""
    mounted = old_tree(tmp / "pgdata-mounted")
    (mounted / "mnt").mkdir()
    spaced = old_tree(tmp / "pgdata-space")
    (spaced / "a b").mkdir()
    exact = old_tree(tmp / "tmp-exact")
    gone = old_tree(tmp / "pgdata-gone")
    sources = []
    for name in ("one", "two", "three"):
        source = tmp.parent / f"source-{name}"
        write(source / "precious", old=48 * 3600)
        sources.append(source)
    mounts = [
        (sources[0], mounted / "mnt"),
        (sources[1], spaced / "a b"),
        (sources[2], exact),
    ]
    for path in (mounted / "mnt", spaced / "a b", mounted, spaced, exact, *sources):
        age(path, 48 * 3600)
    return sources, mounts, gone


@needs_unshare
def test_a_mount_point_under_a_directory_keeps_it(node):
    tmp = node["tmp"]
    sources, mounts, gone = mount_fixture(tmp)
    result = run_with_mounts(node, mounts, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert remaining(tmp) == ["pgdata-mounted", "pgdata-space", "tmp-exact"]
    assert result.stdout.count("keep (mount)") == 3
    assert all((source / "precious").exists() for source in sources)
    assert not gone.exists()


@needs_unshare
def test_a_symlinked_tmp_directory_does_not_hide_a_mount(node, base):
    tmp = node["tmp"]
    sources, mounts, gone = mount_fixture(tmp)
    link = base / "link-to-tmp"
    link.symlink_to(tmp)
    env = dict(node["env"], GC_TMP_DIR=str(link))
    result = run_with_mounts(node, mounts, "--apply", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert remaining(tmp) == ["pgdata-mounted", "pgdata-space", "tmp-exact"]
    assert result.stdout.count("keep (mount)") == 3
    assert f"in {tmp}" in result.stdout  # the real path
    assert all((source / "precious").exists() for source in sources)
    assert not gone.exists()


def test_a_unix_socket_path_keeps_the_directory(node):
    # A short directory keeps the socket path under the AF_UNIX limit.
    tmp = Path(tempfile.mkdtemp(prefix="g", dir="/tmp"))
    env = dict(node["env"], GC_TMP_DIR=str(tmp))
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
        result = run(node, "--apply", env=env)
        survivors = remaining(tmp)
    finally:
        stop(holder)
        subprocess.run(["chmod", "-R", "u+w", str(tmp)], check=False)
        shutil.rmtree(tmp, ignore_errors=True)
    assert result.returncode == 0, result.stderr
    assert survivors == ["pgdata-sock"]
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


def locked_tree(path):
    """An old directory with an unreadable subdirectory that holds a file."""
    old_tree(path)
    inner = path / "private"
    write(inner / "file", old=48 * 3600)
    age(inner, 48 * 3600)
    age(path, 48 * 3600)
    inner.chmod(0)
    return path, inner


def unlock(path):
    subprocess.run(["chmod", "-R", "u+rwX", str(path)], check=False)


def test_an_unreadable_subdirectory_is_kept_in_a_dry_run(node):
    locked, _ = locked_tree(node["tmp"] / "pgdata-locked")
    try:
        result = run(node)
    finally:
        unlock(locked)
    assert result.returncode == 0, result.stderr
    assert locked.exists()
    assert "keep (unreadable; --apply opens it first)" in result.stdout


def test_an_unreadable_subdirectory_does_not_stop_the_run(node):
    tmp = node["tmp"]
    locked, _ = locked_tree(tmp / "pgdata-locked")
    other = old_tree(tmp / "pgdata-other")
    try:
        result = run(node, "--apply")
    finally:
        if locked.exists():
            unlock(locked)
    assert result.returncode == 0, result.stderr
    assert not other.exists()
    assert not locked.exists()
    assert "failed:" not in result.stdout


def test_an_unreadable_subdirectory_does_not_hide_a_newer_file(node):
    tmp = node["tmp"]
    locked, inner = locked_tree(tmp / "pgdata-locked")
    inner.chmod(0o700)
    deep = inner / "deep"
    write(deep / "fresh")  # a file from the last hour, two levels down
    deep.chmod(0)
    age(inner, 48 * 3600)
    age(locked, 48 * 3600)
    inner.chmod(0)
    try:
        result = run(node, "--apply")
        inner_mode = inner.stat().st_mode & 0o777
        inner.chmod(0o100)  # search only, so that deep can be examined
        modes = (inner_mode, deep.stat().st_mode & 0o777)
    finally:
        unlock(locked)
    assert result.returncode == 0, result.stderr
    assert locked.exists()
    assert (deep / "fresh").exists()
    assert "keep (newer file)" in result.stdout
    # The directory stays, so the modes that the check changed are put back.
    assert modes == (0, 0)


def test_a_failed_removal_is_reported_and_the_run_goes_on(node, base):
    tmp = node["tmp"]
    stuck = old_tree(tmp / "pgdata-stuck")
    other = old_tree(tmp / "pgdata-other")
    fake = base / "bin"
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


def test_symlinks_in_a_cache_are_not_followed(node, base):
    outside = base / "outside"
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


@pytest.mark.parametrize(
    "value", ["/", "relative", "{tmp}/", "{home}", "{parent}", "{link_home}"]
)
def test_unsafe_tmp_directories_are_refused(node, base, value):
    link = base / "link-home"
    link.symlink_to(node["home"])
    path = value.format(
        tmp=node["tmp"],
        home=node["home"],
        parent=node["home"].parent,
        link_home=link,
    )
    env = dict(node["env"], GC_TMP_DIR=path)
    result = run(node, "--apply", env=env)
    assert result.returncode == 2
    assert "GC_TMP_DIR" in result.stderr
    assert (node["home"] / ".cache" / "go-build" / "ab" / "old-a").exists()


@pytest.mark.parametrize("suffix", ["", "/", "//"])
def test_a_cache_path_with_trailing_slashes_works(node, suffix):
    env = dict(node["env"], GOCACHE=str(node["go"]) + suffix)
    result = run(node, "--apply", env=env)
    assert result.returncode == 0, result.stderr
    assert "ab/old-a" not in files(node["go"])
    assert "ab/new-a" in files(node["go"])


@pytest.mark.parametrize("where", ["home", "parent"])
def test_a_go_cache_that_holds_the_home_directory_is_refused(node, where):
    # A Go README in the home directory must not make it a cache.
    holder = {"home": node["home"], "parent": node["home"].parent}[where]
    (holder / "README").write_text(GO_README)
    victim = write(node["home"] / "documents" / "old-file", old=90 * DAY)
    for suffix in ("", "/"):
        env = dict(node["env"], GOCACHE=str(holder) + suffix)
        result = run(node, "--apply", env=env)
        assert result.returncode == 0, result.stderr
        assert victim.exists()
        assert "skipped (not a Go build cache, or it holds the home" in result.stdout


needs_root_probe = pytest.mark.skipif(
    shutil.which("unshare") is None or not namespaces_work(),
    reason="no user namespace on this host: root cannot be tested",
)


def run_as_root_namespace(node, base, env, clean_env=False):
    """Run the script as uid 0 in a user namespace, optionally with env -i."""
    command = ["unshare", "-Ur"]
    if clean_env:
        command += ["env", "-i", *[f"{k}={v}" for k, v in env.items()]]
    command += ["bash", str(SCRIPT), "--apply"]
    return subprocess.run(
        command,
        env=None if clean_env else env,
        capture_output=True,
        text=True,
        check=False,
    )


@needs_root_probe
@pytest.mark.parametrize("clean_env", [False, True])
@pytest.mark.parametrize(
    "disguise", ["shim", "function", "euid", "euid and uid", "all of them"]
)
def test_root_is_refused_with_no_exception(node, base, clean_env, disguise):
    # Nothing the caller controls may hide root: a program named id, an
    # exported function named id, or EUID and UID in the environment (bash
    # imports them).
    fake = base / "bin"
    fake.mkdir()
    (fake / "id").write_text("#!/bin/sh\necho 1000\n")
    (fake / "id").chmod(0o755)
    old_tree(node["tmp"] / "pgdata-a")
    before = files(node["tmp"]), files(node["go"]), files(node["bazel"])
    env = dict(node["env"])
    if disguise in ("shim", "all of them"):
        env["PATH"] = f"{fake}:{env['PATH']}"
    if disguise in ("function", "all of them"):
        env["BASH_FUNC_id%%"] = "() { echo 1000; }"
    if disguise in ("euid", "euid and uid", "all of them"):
        env["EUID"] = "1000"
    if disguise in ("euid and uid", "all of them"):
        env["UID"] = "1000"
    result = run_as_root_namespace(node, base, env, clean_env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not as root" in result.stderr
    assert (files(node["tmp"]), files(node["go"]), files(node["bazel"])) == before


@pytest.mark.parametrize("home", ["", "relative"])
def test_home_must_be_an_absolute_path(node, home):
    env = dict(node["env"], HOME=home)
    del env["GOCACHE"]
    result = run(node, "--apply", env=env)
    assert result.returncode == 2
    assert "HOME" in result.stderr


def test_a_double_slash_path_is_collapsed(node):
    old_tree(node["tmp"] / "pgdata-a")
    env = dict(node["env"], GC_TMP_DIR="/" + str(node["tmp"]))
    result = run(node, env=env)
    assert result.returncode == 0, result.stderr
    assert f"Test directories in {node['tmp']} " in result.stdout
    assert "//" not in result.stdout.split("Test directories in")[1].split(" ")[0]


def test_a_proc_root_path_does_not_pass_the_home_guards(node):
    # /proc/self/root/<home> is the home directory by another name.
    alias = "/proc/self/root" + str(node["home"])
    env = dict(node["env"], GC_TMP_DIR=alias)
    result = run(node, "--apply", env=env)
    assert result.returncode == 2
    assert "GC_TMP_DIR" in result.stderr
    (node["home"] / "README").write_text(GO_README)
    victim = write(node["home"] / "documents" / "old-file", old=90 * DAY)
    env = dict(node["env"], GOCACHE=alias)
    result = run(node, "--apply", env=env)
    assert result.returncode == 0, result.stderr
    assert victim.exists()
    assert "skipped (not a Go build cache" in result.stdout


@needs_unshare
def test_the_source_of_a_bind_mount_keeps_the_directory(node, base):
    tmp = node["tmp"]
    source = old_tree(tmp / "pgdata-source")
    inside = old_tree(tmp / "pgdata-inside")
    gone = old_tree(tmp / "pgdata-gone")
    elsewhere = [base / "elsewhere-one", base / "elsewhere-two"]
    for path in elsewhere:
        path.mkdir()
    # One bind mount of the directory, one of a directory inside another one.
    mounts = [(source, elsewhere[0]), (inside / "base", elsewhere[1])]
    result = run_with_mounts(node, mounts, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert remaining(tmp) == ["pgdata-inside", "pgdata-source"]
    assert result.stdout.count("keep (mount source)") == 2
    assert (source / "base" / "1" / "relfile").exists()
    assert (inside / "base" / "1" / "relfile").exists()
    assert not gone.exists()


def test_a_bazel_cache_symlink_to_the_home_directory_is_refused(node, base):
    # A link named disk_cache that points at $HOME must not make it a cache.
    home = node["home"]
    victim = write(home / "ac" / "f", old=90 * DAY)
    link = base / "disk_cache"
    link.symlink_to(home, target_is_directory=True)
    for path in (link, str(link) + "/"):
        env = dict(node["env"], BAZEL_DISK_CACHE=str(path))
        result = run(node, "--apply", env=env)
        assert result.returncode == 0, result.stderr
        assert victim.exists()
        assert "Bazel disk cache" in result.stdout
        assert (
            "skipped (not a disk_cache directory, or it holds the home" in result.stdout
        )


def test_a_bazel_cache_symlink_to_a_real_cache_works(node, base):
    link = base / "disk_cache"
    link.symlink_to(node["bazel"], target_is_directory=True)
    env = dict(node["env"], BAZEL_DISK_CACHE=str(link))
    result = run(node, "--apply", env=env)
    assert result.returncode == 0, result.stderr
    assert files(node["bazel"]) == {"cas/cd/new", "gc/lock", "other/old"}


def test_a_bazel_cache_that_holds_the_home_directory_is_refused(node, base):
    # A directory named disk_cache that holds the home directory.
    holder = base / "holder" / "disk_cache"
    home = holder / "home"
    home.mkdir(parents=True)
    victim = write(holder / "ac" / "f", old=90 * DAY)
    env = dict(node["env"], HOME=str(home), BAZEL_DISK_CACHE=str(holder))
    del env["GOCACHE"]
    result = run(node, "--apply", env=env)
    assert result.returncode == 0, result.stderr
    assert victim.exists()
    assert "skipped (not a disk_cache directory, or it holds the home" in result.stdout


def test_a_bazel_cache_link_to_a_directory_not_named_disk_cache(node, base):
    # The link is named disk_cache, but the real directory is named x.
    real = base / "x"
    victim = write(real / "ac" / "q" / "f", old=90 * DAY)
    link = base / "disk_cache"
    link.symlink_to(real, target_is_directory=True)
    env = dict(node["env"], BAZEL_DISK_CACHE=str(link))
    result = run(node, "--apply", env=env)
    assert result.returncode == 0, result.stderr
    assert victim.exists()
    assert "skipped (not a disk_cache directory" in result.stdout


GIB = 1024**3


def test_the_go_cache_is_capped_oldest_first(node):
    # Sparse files: their size counts, but they take no disk.
    go = node["go"]
    for name, days in (("c1", 2.5), ("c2", 2.0), ("c3", 1.0)):
        path = go / "cd" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.truncate(GIB)
        age(path, int(days * DAY))
    result = run(node, "--apply", "--go-max-gib", "3")
    assert result.returncode == 0, result.stderr
    # 3 GiB plus ab/new-a is over the cap: the oldest entry goes.
    assert files(go) == {"README", "trim.txt", "ab/new-a", "cd/c2", "cd/c3"}
    assert "over 3 GiB" in result.stdout


def test_the_go_cache_under_the_cap_keeps_new_files(node):
    result = run(node, "--apply", "--go-max-gib", "1")
    assert result.returncode == 0, result.stderr
    assert files(node["go"]) == {"README", "trim.txt", "ab/new-a"}


def scratch_node(node):
    root = node["tmp"] / f"claude-{os.geteuid()}"
    project = root / "-home-op-repo"
    old = old_tree(project / "aaaa-old", hours=5 * 24)
    fresh = old_tree(project / "bbbb-fresh", hours=5 * 24)
    write(fresh / "scratchpad" / "new")
    recent = old_tree(project / "cccc-recent", hours=2 * 24)
    return root, project, old, fresh, recent


def test_idle_session_directories_go(node):
    root, project, old, fresh, recent = scratch_node(node)
    held = old_tree(project / "dddd-held", hours=5 * 24)
    (project / "eeee-link").symlink_to(held)
    top = write(root / "loose-file", old=10 * DAY)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=held
    )
    try:
        time.sleep(0.3)
        dry = run(node)
        result = run(node, "--apply")
    finally:
        holder.kill()
        holder.wait()
    assert dry.returncode == 0, dry.stderr
    assert "Session directories" in dry.stdout and ": 1, " in dry.stdout
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in project.iterdir()) == [
        "bbbb-fresh",
        "cccc-recent",
        "dddd-held",
        "eeee-link",
    ]
    assert top.exists()
    assert "keep (in use)" in result.stdout


def test_scratch_days_option(node):
    _, project, old, fresh, recent = scratch_node(node)
    result = run(node, "--apply", "--scratch-days", "1")
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in project.iterdir()) == ["bbbb-fresh"]


@pytest.mark.parametrize("value", ["{home}", "{parent}", "missing"])
def test_unsafe_session_roots_are_skipped(node, base, value):
    root = {
        "{home}": str(node["home"]),
        "{parent}": str(node["home"].parent),
        "missing": str(base / "missing"),
    }[value]
    old_tree(node["home"] / "proj" / "sess", hours=10 * 24)
    env = dict(node["env"], GC_SCRATCH_DIR=root)
    result = run(node, "--apply", env=env)
    assert result.returncode == 0, result.stderr
    assert "Session directories" in result.stdout and "skipped" in result.stdout
    assert (node["home"] / "proj" / "sess").exists()
