"""Regression tests for the developer tools image layers."""

import functools
import os
import pathlib
import stat
import struct
import subprocess
import tarfile
import tempfile

import pytest


REQUIRED_COMMANDS = {
    "agent-run",
    "bb",
    "buildifier",
    "claude",
    "eslint",
    "hf2oci",
    "shellcheck",
}
NATIVE_COMMANDS = REQUIRED_COMMANDS - {"eslint"}


def _runfile(name: str) -> pathlib.Path:
    matches = list(pathlib.Path(os.environ["TEST_SRCDIR"]).rglob(name))
    assert len(matches) == 1, f"expected one runfile named {name}, got {matches}"
    return matches[0]


def _layers(platform: str) -> list[pathlib.Path]:
    return [
        _runfile(f"tools_tar_{platform}.tar"),
        _runfile(f"source_tools_tar_untransitioned_{platform}.tar"),
        _runfile(f"node_tar_{platform}.tar"),
        _runfile("eslint_node_modules.tar"),
        _runfile("eslint_wrapper.tar"),
    ]


@functools.cache
def _members(layer: pathlib.Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(layer, "r:*") as archive:
        return {
            member.name.removeprefix("./").lstrip("/"): member
            for member in archive.getmembers()
        }


def _read_member(layer: pathlib.Path, name: str, limit: int | None = None) -> bytes | None:
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


@pytest.mark.parametrize("platform", ["linux_amd64", "linux_arm64", "darwin_arm64"])
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

    eslint_payload = _read_member(commands["eslint"], "usr/bin/eslint")
    assert eslint_payload is not None
    assert b'../local/lib/node_modules/eslint/bin/eslint.js' in eslint_payload
    assert b'exec "$ROOT/usr/bin/node"' in eslint_payload

    combined_members = set().union(*(_members(layer) for layer in layers))
    assert "usr/bin/node" in combined_members
    assert "usr/local/lib/node_modules/eslint/package.json" in combined_members


def test_linux_commands_execute_from_relocated_root() -> None:
    layers = _layers("linux_amd64")

    with tempfile.TemporaryDirectory() as temp:
        root = pathlib.Path(temp)
        for layer in layers:
            with tarfile.open(layer, "r:*") as archive:
                archive.extractall(root, filter="data")

        home = root / "home"
        home.mkdir()
        environment = {
            "HOME": str(home),
            "PATH": f"{root / 'usr/bin'}:/usr/bin:/bin",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        invocations = {
            "agent-run": ["--help"],
            # The bb-only version help path validates the CLI without asking
            # its embedded Bazelisk to fetch the repository's Bazel version.
            "bb": ["version", "--help"],
            "buildifier": ["--version"],
            "claude": ["--version"],
            "eslint": ["--version"],
            "hf2oci": ["--help"],
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
