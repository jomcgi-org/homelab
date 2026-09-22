"""Guards on the ENOSPC reclaim gate in the noded rootfs builder (#4458).

The reclaim deletes abandoned build intermediates on a wedged brick, so what is
worth pinning is that it ships disarmed, that its stop condition can actually
evaluate, and above all that it never widens beyond orphans into selecting
among published bases.

Lives in this package rather than in the chart package because a chart package
has no Python toolchain context; it reads the chart files through a
cross-package data dep, the same way cleartext_lane_guard_test reads the deploy
values.
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

DIGITS = re.compile(r"^[0-9]+$")


def _repo_path(*parts: str) -> Path:
    """Resolve a repo-relative path, in-bazel (TEST_SRCDIR) or standalone."""
    rel = Path(*parts)
    candidate = Path(os.environ.get("TEST_SRCDIR", "")) / "_main" / rel
    if candidate.exists():
        return candidate
    # Direct run: this file lives at projects/embervm/runtimes/claude/.
    here = Path(__file__).resolve().parents[4] / rel
    if here.exists():
        return here
    raise FileNotFoundError(f"{rel} not found at {candidate} or {here}")


def _chart_values() -> dict:
    return yaml.safe_load(_repo_path("projects/embervm/chart/values.yaml").read_text())


def test_target_free_bytes_is_a_quoted_string():
    """An unquoted integer here renders in scientific notation and breaks the stop condition.

    Helm promotes a large unquoted YAML integer to a float, so
    `targetFreeBytes: 2147483648` reaches the container as "2.147483648e+09".
    The script compares with `[ "$free" -ge "$target" ]`, which then errors with
    "integer expression expected" and returns 2 rather than a truthy or falsy
    result, so the loop never breaks. In a deletion loop, a stop condition that
    can never fire means delete every candidate instead of stopping once the
    target is met. Keeping the value a QUOTED string is what prevents the
    coercion, so this asserts the YAML type and not just the digits.
    """
    reclaim = _chart_values()["rootfsReclaim"]
    target = reclaim["targetFreeBytes"]
    assert isinstance(target, str), (
        "rootfsReclaim.targetFreeBytes must be a quoted string, got %r (%s). "
        "An unquoted int is rendered by Helm as scientific notation, which the "
        "shell comparison in the rootfs builder cannot parse."
        % (target, type(target).__name__)
    )
    assert DIGITS.match(target), (
        "rootfsReclaim.targetFreeBytes must be plain digits, got %r" % target
    )


def _deploy_values() -> dict:
    return yaml.safe_load(_repo_path("projects/embervm/deploy/values.yaml").read_text())


def _effective(key: str):
    """Chart default, overridden by the deploy overlay when it sets the key.

    The chart default is not what runs. Arming happens in the overlay, so the
    guards below have to judge the effective value, the way cleartext_lane_guard
    reads the deploy values rather than the chart's.
    """
    chart = _chart_values()["rootfsReclaim"]
    deploy = (_deploy_values() or {}).get("rootfsReclaim") or {}
    return deploy[key] if key in deploy else chart[key]


def _size_to_bytes(text: str) -> int:
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    text = str(text).strip()
    if text and text[-1].upper() in units:
        return int(float(text[:-1]) * units[text[-1].upper()])
    return int(text)


def test_target_free_bytes_exceeds_the_bake_size():
    """A target below the rootfs size livelocks the very wedge this exists to break.

    The same constant is BOTH the loop's stop condition and the ENOSPC floor. Set
    it under the bake size and reclaim frees one orphan, stops, fails the retry,
    and on the next restart sees free space already above target and breaks
    before deleting anything. The brick then crashloops forever with reclaimable
    garbage on disk, which is the original incident with extra steps. It also
    truncates the dry-run manifest that arming is decided from.
    """
    target = int(_effective("targetFreeBytes"))
    bake = _size_to_bytes(_chart_values()["rootfsBuilder"]["rootfsSize"])
    assert target > bake, (
        "rootfsReclaim.targetFreeBytes (%d) must exceed rootfsBuilder.rootfsSize "
        "(%s = %d bytes) with headroom for ext4 metadata, or reclaim stops short of "
        "what the retry bake needs and the brick crashloops with garbage still on disk."
        % (target, _chart_values()["rootfsBuilder"]["rootfsSize"], bake)
    )


def test_bake_format_version_is_a_safe_explicit_cache_input():
    builder = _chart_values()["rootfsBuilder"]
    assert builder["bakeFormatVersion"] == "b2"
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,31}", builder["bakeFormatVersion"])


def test_gate_value_is_a_recognised_setting():
    """Only "" and "1" mean anything; anything else silently reads as disarmed.

    Checks the EFFECTIVE value, so a typo in the deploy overlay (the file where
    arming happens) cannot look armed while behaving as disarmed.
    """
    assert _effective("enabled") in ("", "1")


def test_reclaim_ships_disarmed_by_default():
    """The chart default must never delete. Arming is a deliberate act on deploy values.

    This mirrors how baseRetention was armed: read a live dry-run manifest
    first, then set the gate. A chart that defaulted to armed would delete base
    bundles on any cluster installing it without an override.
    """
    assert _chart_values()["rootfsReclaim"]["enabled"] == ""


def test_reclaim_stays_orphans_only():
    """The reclaim must never select among published bases, only abandoned intermediates.

    A first draft picked the newest base per workload and deleted the rest. It
    was wrong three ways: a `.building` staging orphan looked newest so the LIVE
    base became the candidate, a failed stat won the newest slot so a vanished
    path was retained instead, and equal mtimes resolved by directory order.
    Selecting among published artifacts requires knowing which is current, and
    that is the control plane's job, not a bash script's on a shared hostPath
    with no lock.

    So this asserts the SHAPE of the candidate set rather than any comment: file
    candidates must require both the rootfs- prefix and the .tmp. infix, and
    directory candidates must require the .building suffix. If someone
    reintroduces workload-grouped selection, the negative assertion below fails.
    """
    script = _repo_path(
        "projects/embervm/chart/templates/noded-rootfs-builder-configmap.yaml"
    ).read_text()

    assert "-name 'rootfs-*.ext4.tmp.*'" in script, (
        "bake-temporary candidates must require BOTH the rootfs- prefix and the "
        ".tmp. infix, so a completed rootfs-<digest>.ext4 can never match"
    )
    assert "-name '*.building'" in script, (
        "staging-orphan candidates must require the .building suffix"
    )
    assert "newest_path" not in script and "newest_mtime" not in script, (
        "the reclaim must not select among published bases by mtime; that is the "
        "rejected design that deleted live bases (see the comment in the script)"
    )


def test_min_age_guard_is_present():
    """Age is the only thing separating an abandoned intermediate from a live one.

    Several brick pods share this hostPath and there is no lock, so a temporary
    a co-located bake is writing right now looks identical to one abandoned
    hours ago. Removing the min-age check would make the reclaim delete another
    pod's in-flight bake.
    """
    script = _repo_path(
        "projects/embervm/chart/templates/noded-rootfs-builder-configmap.yaml"
    ).read_text()
    assert "EMBERVM_ROOTFS_RECLAIM_MIN_AGE_SECONDS" in script
    assert "$min_age" in script, "the min-age guard is not applied to candidates"


def test_bake_uses_random_ext4_uuid_and_logs_identity():
    """Every mkfs call must retain its random UUID, and the result must be observable."""
    script = _repo_path(
        "projects/embervm/chart/templates/noded-rootfs-builder-configmap.yaml"
    ).read_text()
    mkfs_calls = [
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("mkfs.ext4 ")
    ]

    assert len(mkfs_calls) == 2, "expected initial and post-reclaim mkfs.ext4 calls"
    for call in mkfs_calls:
        assert " -U " not in call, "mkfs.ext4 must assign a random UUID per bake"
        assert "hash_seed" not in call, (
            "mkfs.ext4 must assign its own directory hash seed"
        )

    assert "skip=1128" in script, "the busybox ext4 superblock UUID read is missing"
    assert (
        "rootfs identity digest=sha256:$digest rootfs_size=$size "
        "bake_format=$bake_format uuid="
    ) in script, "the post-bake identity log is missing"


def test_store_get_and_put_receive_the_same_complete_cache_identity():
    script = _repo_path(
        "projects/embervm/chart/templates/noded-rootfs-builder-configmap.yaml"
    ).read_text()
    identity_flags = (
        '--digest "$digest" --rootfs-size "$size" --bake-format "$bake_format"'
    )
    assert f"rootfs-store get {identity_flags} --out" in script
    assert f"rootfs-store put {identity_flags} --file" in script


def _rootfs_download_harness(tmp_path):
    """Run the actual builder with local fake registry/store executables."""
    source = _repo_path(
        "projects/embervm/chart/templates/noded-rootfs-builder-configmap.yaml"
    ).read_text()
    body = source.split("  build-base-rootfs.sh: |\n", 1)[1].split("{{- end }}", 1)[0]
    script = tmp_path / "builder.sh"
    script.write_text(textwrap.dedent(body))
    binaries = tmp_path / "bin"
    binaries.mkdir()
    mock = f"#!{sys.executable}\n" + textwrap.dedent("""\
        import os, sys, time
        from pathlib import Path
        root = Path(os.environ["TEST_ROOTFS_DIR"])
        if Path(sys.argv[0]).name == "crane":
            assert sys.argv[1] == "digest", sys.argv
            (root / ("digest-" + os.environ["BUILDER_ID"])).touch()
            print("sha256:" + "a" * 64)
        else:
            assert sys.argv[1] == "get", sys.argv
            assert sys.argv[sys.argv.index("--rootfs-size") + 1] == os.environ["ROOTFS_SIZE"]
            assert sys.argv[sys.argv.index("--bake-format") + 1] == os.environ["ROOTFS_BAKE_FORMAT"]
            with (root / "downloads").open("a") as log:
                log.write(os.environ["BUILDER_ID"] + "\\n")
            deadline = time.monotonic() + 15
            while not (root / "release").exists():
                if time.monotonic() > deadline:
                    sys.exit(1)
                time.sleep(0.01)
            Path(sys.argv[sys.argv.index("--out") + 1]).write_bytes(b"x" * 2048)
        """)
    for name in ("crane", "rootfs-store"):
        path = binaries / name
        path.write_text(mock)
        path.chmod(0o755)
    flock = binaries / "flock"
    flock.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent("""\
            import fcntl, sys

            args = sys.argv[1:]
            if "-w" in args:
                print("flock: unrecognized option", file=sys.stderr)
                sys.exit(1)
            if args == ["-n", "9"]:
                try:
                    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    sys.exit(1)
            elif args == ["-u", "9"]:
                fcntl.flock(9, fcntl.LOCK_UN)
            else:
                print(f"unexpected flock arguments: {args}", file=sys.stderr)
                sys.exit(2)
            """)
    )
    flock.chmod(0o755)
    return script, {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "GUEST_IMAGE": "test-guest",
        "ROOTFS_SIZE": "4G",
        "ROOTFS_BAKE_FORMAT": "b2",
        "TEST_ROOTFS_DIR": str(tmp_path),
    }


def _wait_for_file(path):
    import time

    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out waiting for {path}")
        time.sleep(0.01)


def _start_builder(script, env, tmp_path, identifier, base_name=None):
    import subprocess

    return subprocess.Popen(
        ["bash", str(script)],
        env={
            **env,
            "BUILDER_ID": identifier,
            "BASE_ROOTFS_PATH": str(
                tmp_path / "cache" / f"base-{base_name or identifier}.ext4"
            ),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _stop_builders(processes):
    import signal

    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)


@pytest.mark.parametrize("same_path", [False, True])
def test_concurrent_rootfs_builders_download_once_and_share_inode(tmp_path, same_path):
    script, env = _rootfs_download_harness(tmp_path)
    processes = []
    try:
        for index in range(4):
            processes.append(
                _start_builder(
                    script, env, tmp_path, str(index), "shared" if same_path else None
                )
            )
        for index in range(4):
            _wait_for_file(tmp_path / f"digest-{index}")
        _wait_for_file(tmp_path / "downloads")
        (tmp_path / "release").touch()
        for process in processes:
            output, _ = process.communicate(timeout=15)
            assert process.returncode == 0, output
        assert len((tmp_path / "downloads").read_text().splitlines()) == 1
        paths = [
            tmp_path / "cache" / f"base-{'shared' if same_path else i}.ext4"
            for i in range(4)
        ]
        assert len({path.stat().st_ino for path in paths}) == 1
        assert all(path.read_bytes() == b"x" * 2048 for path in paths)
    finally:
        _stop_builders(processes)


def test_size_and_format_changes_miss_without_reusing_legacy_cache(tmp_path):
    script, env = _rootfs_download_harness(tmp_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    legacy = cache_dir / ("rootfs-" + "a" * 12 + ".ext4")
    legacy.write_bytes(b"legacy-digest-only-rootfs")
    (tmp_path / "release").touch()

    def run(identifier, overrides=None):
        process = _start_builder(
            script, {**env, **(overrides or {})}, tmp_path, identifier
        )
        output, _ = process.communicate(timeout=15)
        assert process.returncode == 0, output
        return cache_dir / f"base-{identifier}.ext4"

    first = run("first")
    same = run("same")
    different_size = run("size", {"ROOTFS_SIZE": "8G"})
    different_format = run("format", {"ROOTFS_BAKE_FORMAT": "b3"})

    assert (tmp_path / "downloads").read_text().splitlines() == [
        "first",
        "size",
        "format",
    ]
    assert first.stat().st_ino == same.stat().st_ino
    assert (
        len(
            {
                first.stat().st_ino,
                different_size.stat().st_ino,
                different_format.stat().st_ino,
            }
        )
        == 3
    )
    assert first.read_bytes() == b"x" * 2048
    assert different_size.read_bytes() == b"x" * 2048
    assert different_format.read_bytes() == b"x" * 2048
    assert legacy.read_bytes() == b"legacy-digest-only-rootfs"


def test_rootfs_waiter_recovers_after_builder_process_group_dies(tmp_path):
    import signal

    script, env = _rootfs_download_harness(tmp_path)
    processes = []
    try:
        holder = _start_builder(script, env, tmp_path, "holder")
        processes.append(holder)
        _wait_for_file(tmp_path / "downloads")
        waiter = _start_builder(script, env, tmp_path, "waiter")
        processes.append(waiter)
        _wait_for_file(tmp_path / "digest-waiter")
        os.killpg(holder.pid, signal.SIGKILL)
        holder.communicate(timeout=10)
        (tmp_path / "release").touch()
        output, _ = waiter.communicate(timeout=15)
        assert waiter.returncode == 0, output
        assert (tmp_path / "cache" / "base-waiter.ext4").read_bytes() == b"x" * 2048
        assert (tmp_path / "downloads").read_text().splitlines() == ["holder", "waiter"]
    finally:
        _stop_builders(processes)


def test_rootfs_lock_timeout_fails_without_downloading(tmp_path):
    script, env = _rootfs_download_harness(tmp_path)
    lock = tmp_path / "bin" / "flock"
    lock.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "-w" ]; then\n'
        '    echo "flock: unrecognized option" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "done\n"
        '[ "$*" = "-n 9" ] || exit 2\n'
        'printf x >> "$TEST_ROOTFS_DIR/flock-attempts"\n'
        "exit 1\n"
    )
    lock.chmod(0o755)
    sleep = tmp_path / "bin" / "sleep"
    sleep.write_text(
        '#!/bin/sh\n[ "$*" = "5" ] || exit 2\n'
        'printf x >> "$TEST_ROOTFS_DIR/lock-sleeps"\n'
    )
    sleep.chmod(0o755)
    process = _start_builder(script, env, tmp_path, "timeout")
    try:
        output, _ = process.communicate(timeout=10)
        assert process.returncode == 1
        assert "rootfs cache lock timed out" in output
        assert "flock: unrecognized option" not in output
        assert len((tmp_path / "lock-sleeps").read_text()) == 900 // 5
        assert len((tmp_path / "flock-attempts").read_text()) == 900 // 5 + 1
        assert not (tmp_path / "downloads").exists()
        assert not (tmp_path / "cache" / "base-timeout.ext4").exists()
    finally:
        _stop_builders([process])


def _rootfs_driver(tmp_path: Path) -> tuple[Path, Path]:
    source = _repo_path(
        "projects/embervm/chart/templates/noded-rootfs-builder-configmap.yaml"
    ).read_text()
    body = source.split("  build-rootfs-set.sh: |\n", 1)[1].split(
        "  build-base-rootfs.sh: |\n", 1
    )[0]
    driver = tmp_path / "driver.sh"
    driver.write_text(textwrap.dedent(body))
    builder = tmp_path / "fake-builder.sh"
    builder.write_text(
        f'#!/bin/bash\nexec "{sys.executable}" "{tmp_path / "fake_builder.py"}"\n'
    )
    builder.chmod(0o755)
    return driver, builder


def _run_driver(
    driver: Path,
    builder: Path,
    tmp_path: Path,
    concurrency: str,
    ceiling: str,
    workloads: list[tuple[str, str, str, str]],
) -> subprocess.CompletedProcess[str]:
    busybox = tmp_path / "fake-busybox.py"
    busybox.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        'assert sys.argv[1] == "setsid", sys.argv\n'
        "os.setsid()\n"
        "os.execvpe(sys.argv[2], sys.argv[2:], os.environ)\n"
    )
    busybox.chmod(0o755)
    args = ["bash", str(driver), concurrency, ceiling]
    for workload in workloads:
        args.extend(workload)
    return subprocess.run(
        args,
        env={
            **os.environ,
            "ROOTFS_BUILDER_SCRIPT": str(builder),
            "ROOTFS_BUSYBOX": str(busybox),
            "TEST_DRIVER_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_rootfs_driver_bounds_workers_and_reaps_every_child(tmp_path: Path) -> None:
    driver, builder = _rootfs_driver(tmp_path)
    (tmp_path / "fake_builder.py").write_text(
        textwrap.dedent(
            """\
            import fcntl, json, os, time
            from pathlib import Path

            root = Path(os.environ["TEST_DRIVER_DIR"])
            lock = root / "state.lock"
            lock.touch()
            with lock.open("r+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                state_path = root / "state.json"
                state = json.loads(state_path.read_text()) if state_path.exists() else {"current": 0, "maximum": 0, "done": []}
                state["current"] += 1
                state["maximum"] = max(state["maximum"], state["current"])
                state_path.write_text(json.dumps(state))
            time.sleep(0.2)
            with lock.open("r+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                state = json.loads(state_path.read_text())
                state["current"] -= 1
                state["done"].append(os.environ["GUEST_IMAGE"])
                state_path.write_text(json.dumps(state))
            """
        )
    )
    workloads = [
        (f"workload-{index}", f"image-{index}", f"/cache/{index}.ext4", "128")
        for index in range(5)
    ]
    result = _run_driver(driver, builder, tmp_path, "2", "256", workloads)
    assert result.returncode == 0, result.stdout + result.stderr

    import json

    state = json.loads((tmp_path / "state.json").read_text())
    assert state["maximum"] == 2
    assert state["current"] == 0
    assert sorted(state["done"]) == [f"image-{index}" for index in range(5)]
    assert (
        "rootfs driver complete launched=5 skipped=0 max_concurrency=2" in result.stdout
    )


def test_rootfs_driver_logs_class_skips_and_bakes_missing_memory_fail_closed(
    tmp_path: Path,
) -> None:
    driver, builder = _rootfs_driver(tmp_path)
    (tmp_path / "fake_builder.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        'with (Path(os.environ["TEST_DRIVER_DIR"]) / "built").open("a") as f:\n'
        '    f.write(os.environ["GUEST_IMAGE"] + "\\n")\n'
    )
    result = _run_driver(
        driver,
        builder,
        tmp_path,
        "2",
        "128",
        [
            ("too-large", "image-large", "/cache/large.ext4", "256"),
            ("missing", "image-missing", "/cache/missing.ext4", ""),
            ("malformed", "image-malformed", "/cache/malformed.ext4", "invalid"),
            ("eligible", "image-eligible", "/cache/eligible.ext4", "128"),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert set((tmp_path / "built").read_text().splitlines()) == {
        "image-missing",
        "image-malformed",
        "image-eligible",
    }
    assert (
        "rootfs class filter skip workload=too-large memory_mib=256 class_ceiling_mib=128"
        in result.stdout
    )
    assert (
        "rootfs class filter fail-closed workload=missing memory_mib=missing; baking"
        in result.stdout
    )
    assert (
        "rootfs class filter fail-closed workload=malformed memory_mib=invalid; baking"
        in result.stdout
    )


def test_rootfs_driver_propagates_failure_and_terminates_remaining_children(
    tmp_path: Path,
) -> None:
    driver, builder = _rootfs_driver(tmp_path)
    (tmp_path / "fake_builder.py").write_text(
        textwrap.dedent(
            """\
            import os, signal, sys, time
            from pathlib import Path

            root = Path(os.environ["TEST_DRIVER_DIR"])
            image = os.environ["GUEST_IMAGE"]
            (root / ("started-" + image)).touch()
            if image == "fail":
                time.sleep(0.2)
                sys.exit(7)
            if image == "slow":
                def stop(_signum, _frame):
                    (root / "slow-terminated").touch()
                    sys.exit(143)
                signal.signal(signal.SIGTERM, stop)
                while True:
                    time.sleep(0.05)
            (root / ("completed-" + image)).touch()
            """
        )
    )
    result = _run_driver(
        driver,
        builder,
        tmp_path,
        "2",
        "512",
        [
            ("failure", "fail", "/cache/fail.ext4", "128"),
            ("slow", "slow", "/cache/slow.ext4", "128"),
            ("never", "never", "/cache/never.ext4", "128"),
        ],
    )
    assert result.returncode == 7, result.stdout + result.stderr
    assert (tmp_path / "slow-terminated").exists()
    assert not (tmp_path / "started-never").exists()
    assert "status=7; terminating remaining children" in result.stderr


def test_rootfs_driver_executables_and_flags_match_apko_lock() -> None:
    apko = yaml.safe_load(
        _repo_path(
            "projects/firecracker/substrate/rootfs-builder/apko.yaml"
        ).read_text()
    )
    assert {"bash", "busybox", "coreutils", "crane", "e2fsprogs"}.issubset(
        set(apko["contents"]["packages"])
    )
    assert "util-linux" not in apko["contents"]["packages"]
    lock = _repo_path(
        "projects/firecracker/substrate/rootfs-builder/apko.lock.json"
    ).read_text()
    assert '"name": "bash"' in lock and '"version": "5.3-r12"' in lock
    assert '"name": "busybox"' in lock and '"version": "1.38.0-r1"' in lock
    script = _repo_path(
        "projects/embervm/chart/templates/noded-rootfs-builder-configmap.yaml"
    ).read_text()
    assert 'rootfs_busybox="${ROOTFS_BUSYBOX:-/bin/busybox}"' in script
    assert '"$rootfs_busybox" setsid env' in script
    assert "setsid --" not in script
    assert 'wait -n -p completed_pid "${child_pids[@]}"' in script
