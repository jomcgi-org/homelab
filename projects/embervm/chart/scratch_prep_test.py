"""Executable tests for staged scratch filesystem preparation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


CHART = Path(__file__).resolve().parent
SCRIPT = CHART / "files" / "scratch-prep.sh"


def _write_command(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def prep_env(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    commands = tmp_path / "bin"
    commands.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    image = tmp_path / "scratch.img"
    fstab = tmp_path / "fstab"
    fstab.write_text("# retained\n/dev/keep /keep ext4 defaults 0 0\n")
    paths = {
        "active": state / "active",
        "fail_mount": state / "fail-mount",
        "fail_mkfs": state / "fail-mkfs",
        "fs": state / "filesystem",
        "log": state / "commands.log",
        "marker": scratch / ".scratch-generation",
        "mounted": state / "mounted",
        "image": image,
        "scratch": scratch,
        "fstab": fstab,
    }

    _write_command(
        commands,
        "nsenter",
        '''while [ "$#" -gt 0 ]; do
  case "$1" in
    -t) shift 2 ;;
    -m|--) shift ;;
    *) break ;;
  esac
done
exec "$@"''',
    )
    _write_command(
        commands,
        "mountpoint",
        '[ -f "$FAKE_MOUNTED" ]',
    )
    _write_command(
        commands,
        "fallocate",
        '''eval "target=\\${$#}"
if [ -f "$FAKE_REPLACE_ON_FALLOCATE" ]; then
  printf '%s\n' foreign-data > "$FAKE_MANAGED_IMAGE"
  exit 9
fi
: > "$target"
echo fallocate >> "$FAKE_LOG"''',
    )
    for command, filesystem in (("mkfs.ext4", "ext4"), ("mkfs.xfs", "xfs")):
        _write_command(
            commands,
            command,
            f'''eval "target=\\${{$#}}"
echo {command} >> "$FAKE_LOG"
[ ! -f "$FAKE_FAIL_MKFS" ] || exit 9
if [ -f "$FAKE_REPLACE_ON_MKFS" ]; then
  printf '%s\n' foreign-data > "${{FAKE_MANAGED_IMAGE}}.replacement"
  mv "${{FAKE_MANAGED_IMAGE}}.replacement" "$FAKE_MANAGED_IMAGE"
fi
printf '%s\n' formatted-{filesystem} > "$target"
printf '%s\n' {filesystem} > "$FAKE_FS"
rm -f "$SCRATCH_MARKER_PATH"''',
        )
    _write_command(
        commands,
        "blkid",
        '[ -s "$FAKE_FS" ] || exit 2; cat "$FAKE_FS"',
    )
    _write_command(
        commands,
        "mount",
        '''echo mount >> "$FAKE_LOG"
[ ! -f "$FAKE_FAIL_MOUNT" ] || exit 9
touch "$FAKE_MOUNTED"''',
    )
    _write_command(
        commands,
        "umount",
        'rm -f "$FAKE_MOUNTED"; echo umount >> "$FAKE_LOG"',
    )
    _write_command(
        commands,
        "fuser",
        '[ ! -f "$FAKE_ACTIVE" ] || exit 0; exit 1',
    )
    _write_command(
        commands,
        "findmnt",
        """case " $* " in
  *" --target "*) printf '%s %s\n' "${FAKE_MOUNT_SOURCE:-/dev/loop7}" "${FAKE_MOUNT_TYPE:-$(cat "$FAKE_FS")}" ;;
  *" -S "*) printf '%s\n' "${FAKE_MOUNT_TARGETS:-$SCRATCH_PATH}" ;;
  *) exit 2 ;;
esac""",
    )
    _write_command(
        commands,
        "losetup",
        """case " $* " in
  *" -j "*)
    if [ -f "$FAKE_REPLACE_ON_LOSETUP" ]; then
      printf '%s\n' foreign-data > "${FAKE_MANAGED_IMAGE}.replacement"
      mv "${FAKE_MANAGED_IMAGE}.replacement" "$FAKE_MANAGED_IMAGE"
    fi
    if [ -f "$FAKE_MOUNTED" ] && [ -n "${FAKE_LOOP_DEVICE:-}" ]; then
      printf '%s: []: (%s)\n' "$FAKE_LOOP_DEVICE" "$HOST_SCRATCH_IMAGE_PATH"
      if [ -n "${FAKE_EXTRA_LOOP_DEVICE:-}" ]; then
        printf '%s: []: (%s)\n' "$FAKE_EXTRA_LOOP_DEVICE" "$HOST_SCRATCH_IMAGE_PATH"
      fi
    elif [ -n "${FAKE_REMAINING_LOOP_DEVICE:-}" ]; then
      printf '%s: []: (%s)\n' "$FAKE_REMAINING_LOOP_DEVICE" "$HOST_SCRATCH_IMAGE_PATH"
    fi
    ;;
  *" -O BACK-FILE "*) printf '%s\n' "${FAKE_BACKING:-$HOST_SCRATCH_IMAGE_PATH}" ;;
  *) exit 2 ;;
esac""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{commands}:{env['PATH']}",
            "NODE_NAME": "test-node",
            "SCRATCH_SIZE_GI": "35",
            "SCRATCH_FILESYSTEM": "ext4",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "false",
            "SCRATCH_PATH": str(scratch),
            "CONTAINER_SCRATCH_PATH": str(scratch),
            "SCRATCH_IMAGE_PATH": str(image),
            "HOST_SCRATCH_IMAGE_PATH": str(image),
            "HOST_FSTAB_PATH": str(fstab),
            "SCRATCH_MARKER_PATH": str(paths["marker"]),
            "FAKE_ACTIVE": str(paths["active"]),
            "FAKE_FAIL_MOUNT": str(paths["fail_mount"]),
            "FAKE_FAIL_MKFS": str(paths["fail_mkfs"]),
            "FAKE_FS": str(paths["fs"]),
            "FAKE_LOG": str(paths["log"]),
            "FAKE_MANAGED_IMAGE": str(image),
            "FAKE_MOUNTED": str(paths["mounted"]),
            "FAKE_REPLACE_ON_FALLOCATE": str(state / "replace-on-fallocate"),
            "FAKE_REPLACE_ON_LOSETUP": str(state / "replace-on-losetup"),
            "FAKE_REPLACE_ON_MKFS": str(state / "replace-on-mkfs"),
            "FAKE_MOUNT_SOURCE": "/dev/loop7",
            "FAKE_MOUNT_TARGETS": str(scratch),
            "FAKE_LOOP_DEVICE": "/dev/loop7",
        }
    )
    return env, paths


def _run(
    env: dict[str, str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check:
        assert result.returncode == 0, (
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _seed(paths: dict[str, Path], filesystem: str, *, mounted: bool = False) -> None:
    paths["image"].write_text("image")
    paths["fs"].write_text(f"{filesystem}\n")
    if mounted:
        paths["mounted"].touch()


def _add_managed_fstab(paths: dict[str, Path], filesystem: str = "ext4") -> None:
    with paths["fstab"].open("a") as stream:
        stream.write(
            f"{paths['image']} {paths['scratch']} {filesystem} loop,defaults 0 0\n"
        )


@pytest.mark.parametrize("filesystem", ["ext4", "xfs"])
def test_absent_image_uses_selected_filesystem(
    prep_env: tuple[dict[str, str], dict[str, Path]], filesystem: str
) -> None:
    env, paths = prep_env
    env["SCRATCH_FILESYSTEM"] = filesystem
    _run(env)
    assert paths["fs"].read_text().strip() == filesystem
    assert f"mkfs.{filesystem}" in paths["log"].read_text()
    assert paths["marker"].read_text().startswith("test-node-")
    assert f" {filesystem} loop,defaults 0 0" in paths["fstab"].read_text()


def test_existing_ext4_is_not_reformatted_by_xfs_selection_alone(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    _seed(paths, "ext4")
    env["SCRATCH_FILESYSTEM"] = "xfs"
    _run(env)
    assert "mkfs" not in paths["log"].read_text()
    assert " ext4 loop,defaults 0 0" in paths["fstab"].read_text()


def test_opted_in_managed_migration_reconciles_fstab_and_marker(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    _seed(paths, "ext4", mounted=True)
    paths["marker"].write_text("old-generation\n")
    paths["fstab"].write_text(
        "# retained\n"
        "/dev/keep /keep ext4 defaults 0 0\n"
        f"{paths['image']} {paths['scratch']} ext4 loop,defaults 0 0\n"
        f"{paths['image']} {paths['scratch']} ext4 loop,defaults 0 0\n"
    )
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
        }
    )
    _run(env)
    lines = paths["fstab"].read_text().splitlines()
    managed = [line for line in lines if str(paths["image"]) in line]
    assert managed == [f"{paths['image']} {paths['scratch']} xfs loop,defaults 0 0"]
    assert "/dev/keep /keep ext4 defaults 0 0" in lines
    assert paths["marker"].read_text() != "old-generation\n"
    assert paths["fs"].read_text().strip() == "xfs"

    # A rerun preserves both the sole managed fstab entry and generation.
    generation = paths["marker"].read_text()
    _run(env)
    assert paths["marker"].read_text() == generation
    assert paths["fstab"].read_text().splitlines().count(managed[0]) == 1


def test_existing_xfs_rerun_never_reformats(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    _seed(paths, "xfs", mounted=True)
    paths["marker"].write_text("keep-generation\n")
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
        }
    )
    _run(env)
    assert not paths["log"].exists()
    assert paths["marker"].read_text() == "keep-generation\n"


def test_node4_style_bind_is_untouched(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    paths["mounted"].touch()
    paths["marker"].write_text("bind-generation\n")
    env["FAKE_LOOP_DEVICE"] = ""
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
        }
    )
    _run(env)
    assert not paths["log"].exists()
    assert paths["mounted"].exists()
    assert paths["marker"].read_text() == "bind-generation\n"


def test_foreign_mount_identity_is_untouched(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    _seed(paths, "ext4", mounted=True)
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
            "FAKE_MOUNT_SOURCE": "/dev/nvme0n1",
        }
    )
    _run(env)
    assert not paths["log"].exists()
    assert paths["mounted"].exists()


def test_active_consumer_fails_closed_before_unmount(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    _seed(paths, "ext4", mounted=True)
    _add_managed_fstab(paths)
    paths["active"].touch()
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
        }
    )
    result = _run(env, check=False)
    assert result.returncode != 0
    assert "active consumer" in result.stderr
    assert paths["mounted"].exists()
    assert not paths["log"].exists()


def test_mounted_migration_refuses_loop_alias_remaining_after_unmount(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    _seed(paths, "ext4", mounted=True)
    _add_managed_fstab(paths)
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
            "FAKE_REMAINING_LOOP_DEVICE": "/dev/loop7",
        }
    )

    result = _run(env, check=False)

    assert result.returncode != 0
    assert "still has a loop alias" in result.stderr
    assert paths["fs"].read_text().strip() == "ext4"
    assert "mkfs.xfs" not in paths["log"].read_text()
    assert not paths["marker"].exists()


@pytest.mark.parametrize(
    "fstab_entry",
    [
        "/dev/nvme-test {scratch} xfs defaults 0 0\n",
        "{image} /foreign-target ext4 loop,defaults 0 0\n",
    ],
)
def test_new_image_rejects_foreign_fstab_identity_before_allocation(
    prep_env: tuple[dict[str, str], dict[str, Path]], fstab_entry: str
) -> None:
    env, paths = prep_env
    paths["fstab"].write_text(
        fstab_entry.format(image=paths["image"], scratch=paths["scratch"])
    )

    result = _run(env, check=False)

    assert result.returncode != 0
    assert "fstab contains a foreign or aliased entry" in result.stderr
    assert not paths["image"].exists()
    assert not paths["log"].exists()


def test_fstab_inspection_error_is_not_reported_as_unsafe_identity(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    paths["fstab"].unlink()

    result = _run(env, check=False)

    assert result.returncode != 0
    assert "cannot inspect fstab identity" in result.stderr
    assert "fstab contains a foreign or aliased entry" not in result.stderr
    assert not paths["image"].exists()
    assert not paths["log"].exists()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"FAKE_EXTRA_LOOP_DEVICE": "/dev/loop8"}, "multiple loop aliases"),
        ({"FAKE_BACKING": "/foreign.img"}, "loop backing identity"),
        (
            {"FAKE_MOUNT_TARGETS": "/var/lib/embervm/other"},
            "foreign or additional mount",
        ),
        ({"FAKE_MOUNT_TYPE": "xfs"}, "filesystem types disagree"),
    ],
)
def test_mounted_migration_rejects_identity_disagreement(
    prep_env: tuple[dict[str, str], dict[str, Path]],
    override: dict[str, str],
    message: str,
) -> None:
    env, paths = prep_env
    _seed(paths, "ext4", mounted=True)
    _add_managed_fstab(paths)
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
            **override,
        }
    )

    result = _run(env, check=False)

    assert result.returncode != 0
    assert message in result.stderr
    assert paths["fs"].read_text().strip() == "ext4"
    assert paths["mounted"].exists()
    assert not paths["log"].exists()


def test_unmounted_migration_requires_managed_fstab_identity(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    _seed(paths, "ext4")
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
            "FAKE_LOOP_DEVICE": "",
        }
    )
    result = _run(env, check=False)
    assert result.returncode != 0
    assert "fstab does not identify" in result.stderr
    assert paths["fs"].read_text().strip() == "ext4"
    assert not paths["log"].exists()


def test_migration_refuses_hard_link_alias(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    _seed(paths, "ext4")
    _add_managed_fstab(paths)
    paths["image"].with_name("scratch-alias.img").hardlink_to(paths["image"])
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
            "FAKE_LOOP_DEVICE": "",
        }
    )

    result = _run(env, check=False)

    assert result.returncode != 0
    assert "hard-link aliases" in result.stderr
    assert paths["fs"].read_text().strip() == "ext4"
    assert not paths["log"].exists()


def test_migration_never_formats_concurrent_path_replacement(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    _seed(paths, "ext4")
    _add_managed_fstab(paths)
    Path(env["FAKE_REPLACE_ON_MKFS"]).touch()
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
            "FAKE_LOOP_DEVICE": "",
        }
    )

    result = _run(env, check=False)

    assert result.returncode != 0
    assert "identity changed during migration" in result.stderr
    assert paths["image"].read_text() == "foreign-data\n"
    assert not paths["mounted"].exists()
    assert not paths["marker"].exists()


@pytest.mark.parametrize("mounted", [False, True])
def test_migration_never_formats_replacement_during_verification(
    prep_env: tuple[dict[str, str], dict[str, Path]], mounted: bool
) -> None:
    env, paths = prep_env
    _seed(paths, "ext4", mounted=mounted)
    _add_managed_fstab(paths)
    Path(env["FAKE_REPLACE_ON_LOSETUP"]).touch()
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
        }
    )
    if not mounted:
        env["FAKE_LOOP_DEVICE"] = ""

    result = _run(env, check=False)

    assert result.returncode != 0
    assert "identity changed during migration verification" in result.stderr
    assert paths["image"].read_text() == "foreign-data\n"
    assert paths["fs"].read_text().strip() == "ext4"
    assert not paths["log"].exists()
    assert not paths["marker"].exists()


def test_symlink_image_is_rejected_without_touching_target(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    foreign = paths["image"].with_name("foreign.img")
    foreign.write_text("foreign-data")
    paths["image"].symlink_to(foreign)
    env["SCRATCH_FILESYSTEM"] = "xfs"

    result = _run(env, check=False)
    assert result.returncode != 0
    assert "non-symlink" in result.stderr
    assert foreign.read_text() == "foreign-data"
    assert not paths["log"].exists()


def test_format_failure_does_not_publish_readiness_marker(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    paths["fail_mkfs"].touch()
    env["SCRATCH_FILESYSTEM"] = "xfs"
    result = _run(env, check=False)
    assert result.returncode != 0
    assert not paths["image"].exists()
    assert not paths["marker"].exists()
    assert not paths["mounted"].exists()


def test_migration_mount_failure_leaves_xfs_fstab_for_reboot(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    _seed(paths, "ext4")
    _add_managed_fstab(paths)
    paths["fail_mount"].touch()
    env.update(
        {
            "SCRATCH_FILESYSTEM": "xfs",
            "SCRATCH_MIGRATE_EXT4_TO_XFS": "true",
            "FAKE_LOOP_DEVICE": "",
        }
    )

    result = _run(env, check=False)

    assert result.returncode != 0
    assert paths["fs"].read_text().strip() == "xfs"
    assert (
        f"{paths['image']} {paths['scratch']} xfs loop,defaults 0 0"
        in paths["fstab"].read_text()
    )
    assert not paths["mounted"].exists()
    assert not paths["marker"].exists()


def test_creation_failure_never_removes_foreign_replacement(
    prep_env: tuple[dict[str, str], dict[str, Path]],
) -> None:
    env, paths = prep_env
    Path(env["FAKE_REPLACE_ON_FALLOCATE"]).touch()

    result = _run(env, check=False)

    assert result.returncode != 0
    assert paths["image"].read_text() == "foreign-data\n"
    assert not paths["marker"].exists()


@pytest.mark.parametrize(
    ("extra_args", "filesystem", "migration"),
    [
        ([], "ext4", "false"),
        (["--set", "scratchPrep.filesystem=ext4"], "ext4", "false"),
        (
            [
                "--set",
                "scratchPrep.filesystem=xfs",
                "--set",
                "scratchPrep.migrateExt4ToXfs=true",
            ],
            "xfs",
            "true",
        ),
    ],
)
def test_rendered_gate_combinations(
    extra_args: list[str], filesystem: str, migration: str
) -> None:
    helm = os.environ.get("HELM_BIN", "helm")
    # HELM_BIN is a Bazel-pinned runfile and argv is passed without a shell.
    result = subprocess.run(
        [  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            helm,
            "template",
            "scratch-test",
            str(CHART),
            *extra_args,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    documents = [
        yaml.safe_load(doc) for doc in result.stdout.split("\n---") if doc.strip()
    ]
    daemonset = next(
        doc
        for doc in documents
        if doc.get("kind") == "DaemonSet"
        and doc.get("metadata", {}).get("name", "").endswith("scratch-prep")
    )
    configmap = next(
        doc
        for doc in documents
        if doc.get("kind") == "ConfigMap"
        and doc.get("metadata", {}).get("name", "").endswith("scratch-prep-script")
    )
    container = daemonset["spec"]["template"]["spec"]["containers"][0]
    rendered_env = {item["name"]: item.get("value") for item in container["env"]}
    assert rendered_env["SCRATCH_FILESYSTEM"] == filesystem
    assert rendered_env["SCRATCH_MIGRATE_EXT4_TO_XFS"] == migration
    host_mount = next(
        item for item in container["volumeMounts"] if item["name"] == "host-embervm"
    )
    assert host_mount["mountPropagation"] == "HostToContainer"
    script_path = "/opt/embervm/scratch-prep.sh"
    assert configmap["data"]["scratch-prep.sh"] == SCRIPT.read_text().rstrip("\n")
    assert container["command"] == [
        "/bin/sh",
        "-c",
        f"/bin/sh {script_path} && exec sleep infinity",
    ]
