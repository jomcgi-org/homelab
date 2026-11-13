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
        '[ "$2" = "$SCRATCH_PATH" ] && [ -f "$FAKE_MOUNTED" ]',
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


# Offline fixtures deliberately use a synthetic node name. No overlay or
# Application references these settings; activation needs a separate delivery.
def _trial_values() -> dict:
    return {
        "scratchSizeGi": 4,
        "workloads": {
            name: {
                "rootfsPath": workload["rootfsPath"].replace(
                    "/var/lib/embervm/scratch/", "/var/lib/embervm-xfs-trial/scratch/"
                )
            }
            for name, workload in yaml.safe_load((CHART / "values.yaml").read_text())[
                "workloads"
            ].items()
        },
        "scratchPrep": {
            "enabled": True,
            "filesystem": "xfs",
            "isolatedDevTrial": {"enabled": True, "nodeName": "offline-node"},
        },
        "noded": {
            "enabled": False,
            "nodeSelector": {
                "cloud.google.com/gke-nodepool": "ember-bricks",
                "homelab.io/firecracker": "true",
            },
            "firecracker": {
                "nvmeRoot": "/var/lib/embervm-xfs-trial/scratch",
                "nvmeRootHostPathType": "Directory",
                "sharedScratch": {"enabled": False},
            },
            "store": {"endpoint": "", "bucket": "", "credentials": {"enabled": False}},
            "bearerTokenSecret": {"enabled": False},
        },
        "bricks": {"enabled": True, "nodeFloors": []},
        "kekRoot": {"enabled": False},
        "tokenBroker": {"enabled": False},
        "egress": {"enabled": False},
        "opLog": {"postgres": {"enabled": False}},
        "rbac": {"scope": "namespace"},
        "conformance": {"codex": {"enabled": False}},
    }


def _render_trial(tmp_path, values, namespace="embervm-dev"):
    values_path = tmp_path / "trial.yaml"
    values_path.write_text(yaml.safe_dump(values))
    return subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "embervm-dev",
            str(CHART),
            "--namespace",
            namespace,
            "-f",
            str(values_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_trial_rendered_paths_and_node_boundary(tmp_path):
    result = _render_trial(tmp_path, _trial_values())
    assert result.returncode == 0, result.stderr
    docs = list(yaml.safe_load_all(result.stdout))
    prep = next(d for d in docs if d and d["kind"] == "DaemonSet")
    pod = prep["spec"]["template"]["spec"]
    affinity = pod["affinity"]
    assert affinity == {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchFields": [
                            {
                                "key": "metadata.name",
                                "operator": "In",
                                "values": ["offline-node"],
                            }
                        ]
                    }
                ]
            }
        }
    }
    assert pod["automountServiceAccountToken"] is False
    assert pod["volumes"][0]["hostPath"]["path"] == "/var/lib/embervm-xfs-trial"
    container = pod["containers"][0]
    env = {v["name"]: v.get("value") for v in container["env"]}
    root = "/var/lib/embervm-xfs-trial/scratch"
    assert env["SCRATCH_PATH"] == root
    assert env["HOST_SCRATCH_IMAGE_PATH"] == root + ".img"
    assert env["SCRATCH_IMAGE_PATH"] == "/host/scratch.img"
    assert env["CONTAINER_SCRATCH_PATH"] == "/host/scratch"
    assert env["SCRATCH_MARKER_PATH"] == root + "/.scratch-generation"
    assert env["SCRATCH_SIZE_GI"] == "4"
    assert (
        f'mountpoint -q {root} && test -s "{root}/.scratch-generation"'
        in container["readinessProbe"]["exec"]["command"][-1]
    )
    bricks = [
        d
        for d in docs
        if d and d["kind"] == "Deployment" and "brick-" in d["metadata"]["name"]
    ]
    assert bricks
    for brick in bricks:
        spec = brick["spec"]["template"]["spec"]
        assert spec["affinity"] == affinity
        assert next(v for v in spec["volumes"] if v["name"] == "nvme")["hostPath"] == {
            "path": root,
            "type": "Directory",
        }
        assert root + "/.scratch-generation" in yaml.safe_dump(spec)
        assert spec["serviceAccountName"].startswith("embervm-dev-")
        assert "/var/lib/embervm/scratch" not in yaml.safe_dump(spec)
    assert "/var/lib/embervm/scratch" not in yaml.safe_dump(pod)


@pytest.mark.parametrize(
    "path,value",
    [
        ("scratchPrep.isolatedDevTrial.nodeName", ""),
        ("scratchPrep.isolatedDevTrial.nodeName", "*"),
        ("scratchPrep.enabled", False),
        ("scratchPrep.filesystem", "ext4"),
        ("noded.enabled", True),
        ("noded.firecracker.nvmeRoot", "/var/lib/embervm/scratch/dev"),
        ("noded.firecracker.sharedScratch.enabled", True),
        ("noded.firecracker.nvmeRootHostPathType", "DirectoryOrCreate"),
        ("noded.nodeSelector", {}),
        ("bricks.nodeFloors", [{"node": "another-node", "class": "2gi"}]),
        ("noded.store.credentials.enabled", True),
        ("noded.store.endpoint", "https://storage.googleapis.com"),
        ("noded.store.bucket", "embervm"),
        ("noded.bearerTokenSecret.enabled", True),
        ("kekRoot.enabled", True),
        ("tokenBroker.enabled", True),
        ("egress.enabled", True),
        ("opLog.postgres.enabled", True),
        ("rbac.scope", "cluster"),
        (
            "workloads.sandboxPython.rootfsPath",
            "/var/lib/embervm/scratch/dev/rootfs.ext4",
        ),
        (
            "workloads.sandboxPython.rootfsPath",
            "/var/lib/embervm-xfs-trial/scratch/../rootfs.ext4",
        ),
    ],
)
def test_trial_rejects_incomplete_configuration(tmp_path, path, value):
    values = _trial_values()
    target = values
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    result = _render_trial(tmp_path, values)
    assert result.returncode != 0


@pytest.mark.parametrize("size", [None, 0, -1, 9, 35, 70, 1.5, "1G", True])
def test_trial_rejects_implicit_or_unbounded_size(tmp_path, size):
    values = _trial_values()
    if size is None:
        del values["scratchSizeGi"]  # Chart default must not activate the trial.
    else:
        values["scratchSizeGi"] = size
    result = _render_trial(tmp_path, values)
    assert result.returncode != 0
    assert "isolatedDevTrial requires explicit scratchSizeGi of 1 through 8 GiB" in (
        result.stderr
    )


@pytest.mark.parametrize("size", [1, 8])
def test_trial_accepts_bounded_size(tmp_path, size):
    values = _trial_values()
    values["scratchSizeGi"] = size
    result = _render_trial(tmp_path, values)
    assert result.returncode == 0, result.stderr
    docs = list(yaml.safe_load_all(result.stdout))
    prep = next(d for d in docs if d and d["kind"] == "DaemonSet")
    env = prep["spec"]["template"]["spec"]["containers"][0]["env"]
    assert next(v["value"] for v in env if v["name"] == "SCRATCH_SIZE_GI") == str(size)


def test_trial_rejects_production_namespace(tmp_path):
    assert _render_trial(tmp_path, _trial_values(), "embervm").returncode != 0


@pytest.mark.parametrize(
    "override",
    [
        {"NODE_NAME": "other-node"},
        {"SCRATCH_TRIAL_NODE": ""},
        {"SCRATCH_PATH": "/var/lib/embervm/scratch"},
        {"HOST_SCRATCH_IMAGE_PATH": "/var/lib/embervm/scratch.img"},
        {"SCRATCH_MARKER_PATH": "/var/lib/embervm/scratch/.scratch-generation"},
        {"SCRATCH_FILESYSTEM": "ext4"},
        {"HOST_FSTAB_PATH": "/other/fstab"},
        {"SCRATCH_IMAGE_PATH": "/host/production.img"},
        {"CONTAINER_SCRATCH_PATH": "/host/production"},
    ],
)
def test_rendered_trial_refuses_drift_before_host_commands(
    tmp_path, prep_env, override
):
    result = _render_trial(tmp_path, _trial_values())
    assert result.returncode == 0, result.stderr
    docs = list(yaml.safe_load_all(result.stdout))
    prep = next(d for d in docs if d and d["kind"] == "DaemonSet")
    env, paths = prep_env
    env.update(
        {
            v["name"]: v["value"]
            for v in prep["spec"]["template"]["spec"]["containers"][0]["env"]
            if "value" in v
        }
    )
    env.update(NODE_NAME="offline-node", HOST_FSTAB_PATH="/etc/fstab")
    env.update(override)
    _write_command(
        Path(env["PATH"].split(":")[0]),
        "nsenter",
        'echo unexpected-host-command >> "$FAKE_LOG"; exit 99',
    )
    result = _run(env, check=False)
    assert result.returncode != 0
    assert "isolated trial" in result.stderr
    assert not paths["log"].exists()
    assert not paths["image"].exists()
    assert not paths["marker"].exists()


def test_inactive_trial_profile_has_no_rendered_effect(tmp_path):
    ordinary = _render_trial(tmp_path, {})
    dormant = _render_trial(
        tmp_path,
        {
            "scratchPrep": {
                "isolatedDevTrial": {
                    "enabled": False,
                    "nodeName": "*",
                }
            }
        },
    )
    assert ordinary.returncode == dormant.returncode == 0
    assert ordinary.stdout == dormant.stdout


def test_rendered_trial_executes_existing_provisioner_in_sandbox(tmp_path, prep_env):
    """Translate absolute host/container roots into a tmpdir, never mount a host.

    Everything else is the rendered ConfigMap script and env. Existing command
    doubles implement mkfs/mount/loop inspection and erase the marker on mkfs.
    """
    result = _render_trial(tmp_path, _trial_values())
    assert result.returncode == 0, result.stderr
    docs = list(yaml.safe_load_all(result.stdout))
    prep = next(d for d in docs if d and d["kind"] == "DaemonSet")
    config = next(
        d
        for d in docs
        if d
        and d["kind"] == "ConfigMap"
        and d["metadata"]["name"].endswith("scratch-prep-script")
    )
    env, paths = prep_env

    def sandbox(text):
        return (
            text.replace("/var/lib/embervm-xfs-trial", str(tmp_path))
            .replace("/host", str(tmp_path))
            .replace("/etc/fstab", str(paths["fstab"]))
        )

    script = tmp_path / "rendered-prep.sh"
    script.write_text(sandbox(config["data"]["scratch-prep.sh"]))
    env.update(
        {
            v["name"]: sandbox(v["value"])
            for v in prep["spec"]["template"]["spec"]["containers"][0]["env"]
            if "value" in v
        }
    )
    env["NODE_NAME"] = "offline-node"
    # This double supports the trial's SOURCE-only query as well as migration's
    # SOURCE,FSTYPE query; all other behavior is the shared behavioral harness.
    commands = Path(env["PATH"].split(":")[0])
    findmnt = commands / "findmnt"
    findmnt.write_text(
        findmnt.read_text().replace(
            "set -eu\n",
            'set -eu\nif [ "$*" = "-n -o SOURCE --target $SCRATCH_PATH" ]; then echo "$FAKE_MOUNT_SOURCE"; exit; fi\n',
            1,
        )
    )

    def run(check=True):
        result = subprocess.run(
            ["/bin/sh", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check:
            assert result.returncode == 0, result.stderr
        return result

    run()
    assert paths["fs"].read_text().strip() == "xfs"
    generation = paths["marker"].read_text()
    readiness = prep["spec"]["template"]["spec"]["containers"][0]["readinessProbe"][
        "exec"
    ]["command"]
    readiness = [sandbox(arg) for arg in readiness]
    assert subprocess.run(readiness, env=env, timeout=30).returncode == 0
    paths["marker"].unlink()
    assert subprocess.run(readiness, env=env, timeout=30).returncode != 0
    paths["marker"].write_text(generation)
    assert generation.startswith("offline-node-")
    fstab = paths["fstab"].read_text()
    run()
    assert paths["fstab"].read_text() == fstab
    assert paths["marker"].read_text() == generation
    assert paths["log"].read_text().count("mkfs.xfs") == 1
    env["FAKE_MOUNT_SOURCE"] = "/dev/foreign"
    assert "foreign" in run(False).stderr
    assert paths["marker"].read_text() == generation
    env["FAKE_MOUNT_SOURCE"] = "/dev/loop7"
    # A synthetic ext4 seed exercises the same separately gated migration.
    _seed(paths, "ext4", mounted=True)
    paths["fstab"].write_text(fstab.replace(" xfs ", " ext4 "))
    assert "separate explicit opt-in" in run(False).stderr
    env["SCRATCH_MIGRATE_EXT4_TO_XFS"] = "true"
    run()
    assert paths["marker"].read_text() != generation
    assert paths["fstab"].read_text() == fstab


@pytest.mark.parametrize("lane", ["dev/deploy", "deploy"])
def test_inactive_trial_preserves_hub_values(tmp_path, lane):
    release = "embervm-dev" if lane.startswith("dev") else "embervm"
    args = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        release,
        str(CHART),
        "--namespace",
        release,
    ]
    for filename in ["values.yaml", "values-gke.yaml"]:
        args += ["-f", str(CHART.parent / lane / filename)]
    original = subprocess.run(args, capture_output=True, text=True, timeout=30)
    inactive = subprocess.run(
        args
        + [
            "--set",
            "scratchPrep.isolatedDevTrial.enabled=false",
            "--set",
            "scratchPrep.isolatedDevTrial.nodeName=offline-node",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert original.returncode == inactive.returncode == 0, (
        original.stderr + inactive.stderr
    )
    assert original.stdout == inactive.stdout
    pods = [
        d["spec"]["template"]
        for d in yaml.safe_load_all(original.stdout)
        if d and d["kind"] in ("Deployment", "DaemonSet")
    ]
    assert "/var/lib/embervm-xfs-trial" not in yaml.safe_dump(pods)
    assert "/var/lib/embervm/scratch" in original.stdout


def test_trial_layers_on_current_dev_without_store_identity(tmp_path):
    values = _trial_values()
    path = tmp_path / "trial.yaml"
    path.write_text(yaml.safe_dump(values))
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "embervm-dev",
            str(CHART),
            "--namespace",
            "embervm-dev",
            "-f",
            str(CHART.parent / "dev/deploy/values.yaml"),
            "-f",
            str(CHART.parent / "dev/deploy/values-gke.yaml"),
            "-f",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    docs = list(yaml.safe_load_all(result.stdout))
    items = [d for d in docs if d and d["kind"] == "OnePasswordItem"]
    assert all(
        "embervm-store" not in yaml.safe_dump(d)
        and "embervm-noded-token" not in yaml.safe_dump(d)
        and "embervm-kek-root" not in yaml.safe_dump(d)
        for d in items
    )
    for doc in docs:
        if doc and doc["kind"] == "Deployment" and "brick-" in doc["metadata"]["name"]:
            spec = doc["spec"]["template"]["spec"]
            assert spec["affinity"]["nodeAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]["nodeSelectorTerms"][0]["matchFields"][0]["values"] == ["offline-node"]
            assert "EMBERVM_NODED_STORE_ACCESS_KEY_ID" not in yaml.safe_dump(spec)
