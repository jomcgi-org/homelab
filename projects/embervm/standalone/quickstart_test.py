"""Focused packaging checks for the standalone EmberVM quickstart."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
EMBER_ROOT = ROOT.parent
EXPECTED_CONTINUITY_SHA256 = (
    "bd12054be9fc385984157d27bba1847ab2ceb4a8167db26b3d0c0b99784f31d9"
)
EXPECTED_HELLO_SHA256 = (
    "53ff98ccb09d4d12a629322caac8ee0aee9f77ca69fd08fbc1eee83b7a60230b"
)


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: UniqueKeyLoader, node: yaml.Node, deep: bool = False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise AssertionError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _documents(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [doc for doc in yaml.load_all(stream, Loader=UniqueKeyLoader) if doc]


def test_continuity_fixture_is_reproducible(tmp_path: Path) -> None:
    output = tmp_path / "continuity.zip"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "build-fixtures.py"),
            "--check",
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == EXPECTED_CONTINUITY_SHA256
    assert hashlib.sha256(output.read_bytes()).hexdigest() == EXPECTED_CONTINUITY_SHA256


def test_manifests_pin_checksums_without_embedding_secrets() -> None:
    values = _documents(ROOT / "values.yaml")[0]
    hello = _documents(ROOT / "workloads" / "hello.yaml")[0]
    continuity = _documents(ROOT / "workloads" / "continuity.yaml")[0]
    platform = _documents(ROOT / "platform.yaml")
    upload = _documents(ROOT / "upload-job.yaml")[0]

    assert values["noded"]["store"]["endpoint"] == "http://embervm-minio:9000"
    assert values["noded"]["store"]["credentials"]["enabled"] is True
    assert values["noded"]["bearerTokenSecret"]["enabled"] is True
    assert values["tokenBroker"]["enabled"] is False
    assert values["xds"]["enabled"] is False
    assert values["servingEnvoy"]["enabled"] is False
    assert values["egress"]["enabled"] is False

    assert hello["spec"]["source"]["zip"]["sha256"] == EXPECTED_HELLO_SHA256
    assert continuity["spec"]["source"]["zip"]["sha256"] == EXPECTED_CONTINUITY_SHA256
    assert continuity["spec"]["class"] == "session"
    assert continuity["spec"]["session"]["idleBankSeconds"] == 5

    pvc = next(doc for doc in platform if doc["kind"] == "PersistentVolumeClaim")
    deployment = next(doc for doc in platform if doc["kind"] == "Deployment")
    assert pvc["spec"]["storageClassName"] == "local-path"
    minio_env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    assert all("value" not in item for item in minio_env)
    upload_env = upload["spec"]["template"]["spec"]["containers"][0]["env"]
    secret_env = [item for item in upload_env if item["name"].startswith("MINIO_")]
    assert all("secretKeyRef" in item["valueFrom"] for item in secret_env)


def test_assets_avoid_cluster_domains_digests_and_em_dashes() -> None:
    paths = [
        ROOT / "values.yaml",
        ROOT / "platform.yaml",
        ROOT / "upload-job.yaml",
        ROOT / "install.sh",
        ROOT / "cleanup.sh",
        ROOT / "host-check.sh",
        ROOT / "workloads" / "hello.yaml",
        ROOT / "workloads" / "continuity.yaml",
    ]
    joined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert ".svc.cluster.local" not in joined
    assert "@sha256:" not in joined
    assert "\N{EM DASH}" not in joined
    install_script = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert EXPECTED_HELLO_SHA256 in install_script


def test_standalone_values_render_one_noded_runtime() -> None:
    helm = os.environ.get("HELM_BIN", "helm")
    result = subprocess.run(
        [
            helm,
            "template",
            "embervm",
            str(EMBER_ROOT / "chart"),
            "--namespace",
            "embervm",
            "--values",
            str(ROOT / "values.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert not any(doc["kind"] == "OnePasswordItem" for doc in rendered)
    assert not any("tokenbroker" in doc["metadata"]["name"] for doc in rendered)
    assert not any(doc["metadata"]["name"].endswith("serving") for doc in rendered)

    noded = next(
        doc
        for doc in rendered
        if doc["kind"] == "DaemonSet"
        and doc["metadata"]["name"] == "embervm-embervm-noded"
    )
    pod_spec = noded["spec"]["template"]["spec"]
    init_names = [container["name"] for container in pod_spec["initContainers"]]
    assert init_names == ["build-runtime-python-rootfs"]
    assert pod_spec["priorityClassName"] == "embervm-standalone"

    control = next(
        doc
        for doc in rendered
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "embervm-embervm"
    )
    control_container = next(
        container
        for container in control["spec"]["template"]["spec"]["containers"]
        if container["name"] == "control-plane"
    )
    env = {item["name"]: item for item in control_container["env"]}
    assert env["EMBERVM_ALLOWED_SERVICE_ACCOUNTS"]["value"] == (
        "system:serviceaccount:embervm:embervm-embervm"
    )
    identity = env["EMBERVM_NODE_IMAGE_IDENTITY"]["value"]
    assert identity.count("=") == 1
    assert "/runtime-python/rootfs-sha256-" in identity
    assert identity.endswith(".ext4|/usr/local/bin/ember-runtime-guest-init")
