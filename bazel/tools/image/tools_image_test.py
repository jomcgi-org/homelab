"""Regression tests for the developer tools image layers."""

import functools
import json
import os
import pathlib
import platform as host_platform
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile

import pytest
from python.runfiles import Runfiles


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
SUPPORTED_PLATFORMS = ["linux_amd64", "linux_arm64", "darwin_arm64"]
RUNTIME_PLATFORM = os.environ.get("TOOLS_IMAGE_RUNTIME_PLATFORM")
TEST_PLATFORMS = [RUNTIME_PLATFORM] if RUNTIME_PLATFORM else SUPPORTED_PLATFORMS
ARM64_CPU_TYPE = 0x0100000C
RUNFILES = Runfiles.Create()


@functools.cache
def _runfile(name: str) -> pathlib.Path:
    resolved = RUNFILES.Rlocation(f"homelab/bazel/tools/image/{name}")
    assert resolved is not None, f"unknown runfile: {name}"
    path = pathlib.Path(resolved)
    assert path.is_file(), f"missing runfile: {path}"
    return path


def _layers(platform: str) -> list[pathlib.Path]:
    return [
        _runfile(f"tools_tar_{platform}.tar"),
        _runfile(f"source_tools_tar_{platform}.tar"),
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


def _darwin_cpu_types(payload: bytes) -> set[int]:
    magic = payload[:4]
    thin_magics = {
        b"\xcf\xfa\xed\xfe": "<",
        b"\xfe\xed\xfa\xcf": ">",
    }
    if magic in thin_magics:
        return {struct.unpack_from(f"{thin_magics[magic]}I", payload, 4)[0]}

    fat_magics = {
        b"\xca\xfe\xba\xbe": (">", 20),
        b"\xbe\xba\xfe\xca": ("<", 20),
        b"\xca\xfe\xba\xbf": (">", 32),
        b"\xbf\xba\xfe\xca": ("<", 32),
    }
    assert magic in fat_magics, f"unexpected Mach-O magic: {magic.hex()}"
    endian, entry_size = fat_magics[magic]
    slice_count = struct.unpack_from(f"{endian}I", payload, 4)[0]
    assert 0 < slice_count <= 32
    header_size = 8 + slice_count * entry_size
    assert len(payload) >= header_size
    return {
        struct.unpack_from(f"{endian}I", payload, 8 + index * entry_size)[0]
        for index in range(slice_count)
    }


def _assert_native_format(payload: bytes, platform: str) -> None:
    if platform == "darwin_arm64":
        cpu_types = _darwin_cpu_types(payload)
        assert ARM64_CPU_TYPE in cpu_types, f"Mach-O slices lack ARM64: {cpu_types}"
        return

    assert payload[:4] == b"\x7fELF"
    assert payload[5] == 1, "tools image supports little-endian Linux only"
    expected_machine = 62 if platform == "linux_amd64" else 183
    assert struct.unpack_from("<H", payload, 18)[0] == expected_machine


def _extract_layers(layers: list[pathlib.Path], root: pathlib.Path) -> None:
    for layer in layers:
        with tarfile.open(layer, "r:*") as archive:
            archive.extractall(root, filter="data")


def _resolved_dependency_version(package_dir: pathlib.Path, name: str) -> str:
    dependency = (package_dir / "node_modules" / name).resolve(strict=True)
    return json.loads((dependency / "package.json").read_text())["version"]


def _semver(version: str) -> tuple[int, int, int]:
    core = version.split("-", 1)[0]
    parts = core.split(".")
    assert len(parts) == 3 and all(part.isdigit() for part in parts), version
    return tuple(int(part) for part in parts)


def _assert_caret_dependency_resolves(package_dir: pathlib.Path, name: str) -> str:
    metadata = json.loads((package_dir / "package.json").read_text())
    declared = metadata["dependencies"][name]
    assert declared.startswith("^"), f"unsupported {name} range: {declared}"

    lower = _semver(declared.removeprefix("^"))
    if lower[0]:
        upper = (lower[0] + 1, 0, 0)
    elif lower[1]:
        upper = (0, lower[1] + 1, 0)
    else:
        upper = (0, 0, lower[2] + 1)

    resolved = _resolved_dependency_version(package_dir, name)
    assert lower <= _semver(resolved) < upper, (
        f"{metadata['name']} resolves {name}@{resolved} outside {declared}"
    )
    return resolved


def _assert_loader_dependencies(root: pathlib.Path, platform: str) -> None:
    binaries = [root / "usr/bin" / command for command in NATIVE_COMMANDS]
    binaries.append(root / "usr/bin/node")

    if platform.startswith("linux_"):
        loader_tool = shutil.which("ldd")
        if loader_tool is None:
            pytest.skip("ldd is not available on this executor")
        for binary in binaries:
            result = subprocess.run(
                [loader_tool, str(binary)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
                check=False,
            )
            output = result.stdout.lower()
            if result.returncode == 0:
                assert "not found" not in output, f"{binary.name}: {result.stdout}"
            else:
                assert (
                    "not a dynamic executable" in output
                    or "statically linked" in output
                ), f"{binary.name}: {result.stdout}"
        return

    loader_tool = shutil.which("otool")
    if loader_tool is None:
        pytest.skip("otool is not available on this executor")
    for binary in binaries:
        result = subprocess.run(
            [loader_tool, "-L", str(binary)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, f"{binary.name}: {result.stdout}"
        assert "not found" not in result.stdout.lower(), (
            f"{binary.name}: {result.stdout}"
        )


@pytest.mark.parametrize("platform", TEST_PLATFORMS)
def test_every_platform_contains_executable_commands(platform: str) -> None:
    layers = _layers(platform)
    commands = _command_layers(layers)

    assert commands.keys() == REQUIRED_COMMANDS
    for command, layer in commands.items():
        member = _members(layer)[f"usr/bin/{command}"]
        assert member.isfile()
        assert member.mode & stat.S_IXUSR
        payload = _read_member(layer, f"usr/bin/{command}", 4096)
        assert payload
        if command in NATIVE_COMMANDS:
            _assert_native_format(payload, platform)

    node_layer = next(layer for layer in layers if "usr/bin/node" in _members(layer))
    node_payload = _read_member(node_layer, "usr/bin/node", 4096)
    assert node_payload
    _assert_native_format(node_payload, platform)

    eslint_payload = _read_member(commands["eslint"], "usr/bin/eslint")
    assert eslint_payload is not None
    assert b'exec "$ROOT/usr/bin/node"' in eslint_payload
    assert b'"$ROOT/usr/local/lib/node_modules/eslint/bin/eslint.js"' in eslint_payload

    combined_members = set().union(*(_members(layer) for layer in layers))
    assert "usr/bin/node" in combined_members
    assert "usr/local/lib/node_modules/eslint" in combined_members


def test_eslint_preserves_versioned_dependency_graph_and_lints() -> None:
    platform = RUNTIME_PLATFORM or "linux_amd64"
    with tempfile.TemporaryDirectory() as temp:
        root = pathlib.Path(temp)
        _extract_layers(_layers(platform), root)

        eslint = (root / "usr/local/lib/node_modules/eslint").resolve(strict=True)
        eslint_utils = (eslint / "node_modules/@eslint-community/eslint-utils").resolve(
            strict=True
        )
        espree = (eslint / "node_modules/espree").resolve(strict=True)
        resolved_versions = {
            _assert_caret_dependency_resolves(consumer, "eslint-visitor-keys")
            for consumer in (eslint, eslint_utils, espree)
        }
        assert len(resolved_versions) == 2, (
            "expected two eslint-visitor-keys versions in the pnpm graph, got "
            f"{resolved_versions}"
        )
        assert _resolved_dependency_version(
            eslint, "eslint-visitor-keys"
        ) == _resolved_dependency_version(espree, "eslint-visitor-keys")
        assert _resolved_dependency_version(
            eslint_utils, "eslint-visitor-keys"
        ) != _resolved_dependency_version(eslint, "eslint-visitor-keys")

        (root / "home").mkdir()
        lint_target = root / "relocated-lint-target.js"
        lint_target.write_text("const answer = 42;\nconsole.log(answer);\n")
        result = subprocess.run(
            [
                str(root / "usr/bin/eslint"),
                "--no-config-lookup",
                str(lint_target),
            ],
            cwd=root,
            env={
                "HOME": str(root / "home"),
                "PATH": f"{root / 'usr/bin'}:/usr/bin:/bin",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout


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
            "agent-run": ["--help"],
            # --cli keeps version reporting inside bb instead of starting its
            # embedded Bazelisk and downloading the repository's Bazel.
            "bb": ["version", "--cli"],
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


@pytest.mark.skip(reason="temporary loader-probe CI isolation")
def test_native_loader_dependencies() -> None:
    platform = RUNTIME_PLATFORM or "linux_amd64"
    _assert_native_host(platform)

    with tempfile.TemporaryDirectory() as temp:
        root = pathlib.Path(temp)
        _extract_layers(_layers(platform), root)
        _assert_loader_dependencies(root, platform)
