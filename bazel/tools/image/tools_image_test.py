"""Regression tests for the developer tools image layers."""

import functools
import os
import pathlib
import platform as host_platform
import stat
import struct
import subprocess
import tarfile
import tempfile

import pytest


REQUIRED_COMMANDS = {
    "bb",
    "buildifier",
    "claude",
    "hf2oci",
    "prettier",
    "shellcheck",
}
NODE_WRAPPERS = {"prettier"}
NATIVE_COMMANDS = REQUIRED_COMMANDS - NODE_WRAPPERS
SUPPORTED_PLATFORMS = ["linux_amd64", "linux_arm64", "darwin_arm64"]
RUNTIME_PLATFORM = os.environ.get("TOOLS_IMAGE_RUNTIME_PLATFORM")
TEST_PLATFORMS = [RUNTIME_PLATFORM] if RUNTIME_PLATFORM else SUPPORTED_PLATFORMS


@functools.cache
def _runfile(name: str) -> pathlib.Path:
    root = pathlib.Path(os.environ["TEST_SRCDIR"])
    workspace_names = (
        os.environ.get("TEST_WORKSPACE", ""),
        "_main",
        "homelab",
    )
    candidates = []
    for workspace in dict.fromkeys(workspace_names):
        if not workspace:
            continue
        path = root / workspace / "bazel/tools/image" / name
        candidates.append(path)
        if path.is_file():
            return path
    raise AssertionError(f"missing runfile {name}; checked {candidates}")


def _layers(platform: str) -> list[pathlib.Path]:
    return [
        _runfile(f"tools_tar_{platform}.tar"),
        _runfile(f"source_tools_tar_{platform}.tar"),
        _runfile(f"node_tar_{platform}.tar"),
        _runfile("prettier.tar"),
    ]


@functools.cache
def _members(layer: pathlib.Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(layer, "r:*") as archive:
        return {
            member.name.removeprefix("./").lstrip("/"): member
            for member in archive.getmembers()
        }


def _read_member(
    layer: pathlib.Path, name: str, limit: int | None = None
) -> bytes | None:
    with tarfile.open(layer, "r:*") as archive:
        for member in archive.getmembers():
            if member.name.removeprefix("./").lstrip("/") != name:
                continue
            extracted = archive.extractfile(member)
            return extracted.read(limit) if extracted else None
    return None


def _command_layers(layers: list[pathlib.Path]) -> dict[str, pathlib.Path]:
    found: dict[str, pathlib.Path] = {}
    for layer in layers:
        members = _members(layer)
        for command in REQUIRED_COMMANDS:
            if f"usr/bin/{command}" in members:
                found[command] = layer
    return found


def _assert_native_format(payload: bytes, platform: str) -> None:
    if platform == "darwin_arm64":
        assert payload[:4] in {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}
        return

    assert payload[:4] == b"\x7fELF"
    assert payload[5] == 1, "tools image supports little-endian Linux only"
    expected_machine = 62 if platform == "linux_amd64" else 183
    assert struct.unpack_from("<H", payload, 18)[0] == expected_machine


def _extract_layers(layers: list[pathlib.Path], root: pathlib.Path) -> None:
    for layer in layers:
        with tarfile.open(layer, "r:*") as archive:
            archive.extractall(root, filter="data")


@pytest.mark.parametrize("platform", TEST_PLATFORMS)
def test_source_tools_exclude_retired_agent_run(platform: str) -> None:
    layer = _runfile(f"source_tools_tar_{platform}.tar")
    assert "usr/bin/agent-run" not in _members(layer)


@pytest.mark.parametrize("platform", TEST_PLATFORMS)
def test_every_platform_contains_executable_commands(platform: str) -> None:
    layers = _layers(platform)
    commands = _command_layers(layers)

    assert commands.keys() == REQUIRED_COMMANDS
    for command, layer in commands.items():
        member = _members(layer)[f"usr/bin/{command}"]
        assert member.isfile()
        assert member.mode & stat.S_IXUSR
        payload = _read_member(layer, f"usr/bin/{command}", 32)
        assert payload
        if command in NATIVE_COMMANDS:
            _assert_native_format(payload, platform)

    # bootstrap.sh extracts the image under ~/.cache/homelab-tools, so the
    # prettier wrapper must resolve node and its plugins from its own root.
    prettier_payload = _read_member(commands["prettier"], "usr/bin/prettier")
    assert prettier_payload is not None
    assert b'exec "$ROOT/usr/bin/node"' in prettier_payload
    assert (
        b'"$ROOT/usr/local/lib/node_modules/prettier/bin/prettier.cjs"'
        in prettier_payload
    )
    assert b'NODE_PATH="$ROOT/usr/local/lib/node_modules' in prettier_payload
    assert b" /usr/" not in prettier_payload

    combined_members = set().union(*(_members(layer) for layer in layers))
    assert "usr/bin/node" in combined_members


def _assert_native_host(platform: str) -> None:
    expected_system, expected_machines = {
        "linux_amd64": ("Linux", {"x86_64", "amd64"}),
        "linux_arm64": ("Linux", {"aarch64", "arm64"}),
        "darwin_arm64": ("Darwin", {"arm64"}),
    }[platform]
    assert host_platform.system() == expected_system
    assert host_platform.machine().lower() in expected_machines


def test_commands_execute_from_relocated_root() -> None:
    platform = RUNTIME_PLATFORM or "linux_amd64"
    _assert_native_host(platform)

    with tempfile.TemporaryDirectory() as temp:
        root = pathlib.Path(temp)
        _extract_layers(_layers(platform), root)

        home = root / "home"
        home.mkdir()
        environment = {
            "HOME": str(home),
            "PATH": f"{root / 'usr/bin'}:/usr/bin:/bin",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        invocations = {
            # --cli keeps version reporting inside bb instead of starting its
            # embedded Bazelisk and downloading the repository's Bazel.
            "bb": ["version", "--cli"],
            "buildifier": ["--version"],
            "claude": ["--version"],
            "hf2oci": ["--help"],
            "prettier": ["--version"],
            "shellcheck": ["--version"],
        }
        for command, command_args in invocations.items():
            result = subprocess.run(
                [str(root / "usr/bin" / command), *command_args],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == 0, f"{command}: {result.stdout}"
