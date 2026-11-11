"""Inspect the built noded image without publishing or executing it."""

import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import tarfile

import pytest


def _assert_platform(config):
    assert config["os"] == "linux"
    assert config["architecture"] == "amd64"
    assert config["config"]["Entrypoint"] == ["/usr/local/bin/embervm-noded"]


def _assert_artifacts(members):
    for name in (
        "usr/local/bin/embervm-noded",
        "opt/fc/firecracker",
        "opt/fc/jailer",
        "opt/fc/snapshot-editor",
        "opt/fc/vmlinux.container",
    ):
        member, header = members[name]
        assert member.isfile(), name
        assert member.size > 0, name
        if name != "opt/fc/vmlinux.container":
            assert member.mode & 0o111, name
            assert header[:4] == b"\x7fELF", name
            assert header[5] == 1, name
            assert struct.unpack_from("<H", header, 18)[0] == 62, name


def test_built_image_is_amd64_with_daemon_and_firecracker():
    root = Path(os.environ["NODED_IMAGE"])

    def blob(descriptor):
        algorithm, digest = descriptor["digest"].split(":")
        return root / "blobs" / algorithm / digest

    index = json.loads((root / "index.json").read_text())
    assert len(index["manifests"]) == 1
    descriptor = index["manifests"][0]
    assert descriptor["mediaType"] == "application/vnd.oci.image.manifest.v1+json"
    manifest = json.loads(blob(descriptor).read_text())
    _assert_platform(json.loads(blob(manifest["config"]).read_text()))
    # The two ordinary tar layers follow the Wolfi base. Inspect their actual
    # bytes in the OCI image, so dropping either dependency fails this test.
    members = {}
    for layer in manifest["layers"][-2:]:
        with tarfile.open(blob(layer), "r:*") as archive:
            for member in archive.getmembers():
                if member.isfile():
                    with archive.extractfile(member) as payload:
                        members[member.name.removeprefix("./").lstrip("/")] = (
                            member,
                            payload.read(32),
                        )
    _assert_artifacts(members)


def test_lock_has_only_x86_64_packages_and_current_checksum():
    directory = Path(__file__).parent
    lock = json.loads((directory / "apko.lock.json").read_text())
    assert lock["version"] == "v1"
    checksum = base64.b64encode(
        hashlib.sha256((directory / "apko.yaml").read_bytes()).digest()
    ).decode()
    assert lock["config"]["checksum"] == "sha256-" + checksum
    assert {p["architecture"] for p in lock["contents"]["packages"]} == {"x86_64"}
    assert {r["architecture"] for r in lock["contents"]["repositories"]} == {"x86_64"}


def test_arm64_config_is_rejected():
    with pytest.raises(AssertionError):
        _assert_platform({"os": "linux", "architecture": "arm64"})


@pytest.mark.parametrize(
    "missing",
    [
        "usr/local/bin/embervm-noded",
        "opt/fc/firecracker",
        "opt/fc/jailer",
        "opt/fc/snapshot-editor",
        "opt/fc/vmlinux.container",
    ],
)
def test_missing_artifact_is_rejected(missing):
    names = [
        "usr/local/bin/embervm-noded",
        "opt/fc/firecracker",
        "opt/fc/jailer",
        "opt/fc/snapshot-editor",
        "opt/fc/vmlinux.container",
    ]
    members = {}
    for name in names:
        if name == missing:
            continue
        member = tarfile.TarInfo(name)
        member.size = 32
        member.mode = 0o755
        header = b"\x7fELF\x02\x01" + bytes(12) + struct.pack("<H", 62) + bytes(12)
        members[name] = member, header
    with pytest.raises(KeyError, match=missing):
        _assert_artifacts(members)
