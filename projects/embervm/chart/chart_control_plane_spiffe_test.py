"""Rendered behavior checks for the control plane's optional SPIFFE dial to noded.

Phase 2b of #5706 (#5758). Default off must render byte-identical control-plane
env to before; ``controlPlane.spiffe.enabled`` adds only the sidecar and the
SVID volume; ``dialNoded`` adds the TLS env and must agree, byte for byte, with
the identity noded's own allowlist (PR #6378) derives for the control plane.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parent
_GKE_VALUES = _CHART_DIR.parent / "deploy" / "values-gke.yaml"
_HOME_VALUES = _CHART_DIR.parent / "deploy" / "values.yaml"

_TLS_ENV = {
    "EMBERVM_NODED_DIAL_TLS",
    "EMBERVM_NODED_TLS_PORT",
    "EMBERVM_NODED_SPIFFE_ID",
    "EMBERVM_NODED_SVID_CERT",
    "EMBERVM_NODED_SVID_KEY",
    "EMBERVM_NODED_SVID_BUNDLE",
}


def _render(
    release: str,
    settings: list[str] | None = None,
    values: list[Path] | None = None,
) -> list[dict[str, Any]]:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    argv = [
        helm_bin,
        "template",
        release,
        str(_CHART_DIR),
        "--namespace",
        release,
    ]
    for path in values or []:
        argv += ["--values", str(path)]
    for setting in settings or []:
        argv += ["--set", setting]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _named(items: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in items or []}


def _control_plane(documents: list[dict[str, Any]], release: str) -> dict[str, Any]:
    matches = [
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document["metadata"]["name"] == f"{release}-embervm"
    ]
    assert len(matches) == 1
    return matches[0]["spec"]["template"]["spec"]


def _container(pod: dict[str, Any], name: str) -> dict[str, Any]:
    return next(
        container for container in pod["containers"] if container["name"] == name
    )


def _noded_pods(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pods = []
    for document in documents:
        if document.get("kind") not in {"DaemonSet", "Deployment"}:
            continue
        spec = document["spec"]["template"]["spec"]
        if any(container["name"] == "noded" for container in spec["containers"]):
            pods.append(spec)
    assert pods, "expected at least one rendered noded pod"
    return pods


def _config_map(documents: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    matches = [
        document
        for document in documents
        if document.get("kind") == "ConfigMap" and document["metadata"]["name"] == name
    ]
    assert len(matches) <= 1
    return matches[0] if matches else None


def _file_mode(config: str, name: str) -> int:
    matches = re.findall(
        rf"^[ \t]*{name}[ \t]*=[ \t]*(.*?)[ \t]*$", config, re.MULTILINE
    )
    assert len(matches) == 1, f"expected exactly one {name}: {matches}"
    assert re.fullmatch(r"0[0-7]{3}", matches[0]), f"invalid {name}: {matches[0]}"
    return int(matches[0], 8)


@pytest.mark.parametrize(
    "config",
    [
        "",
        "key_file_mode = ",
        "key_file_mode = 0648",
        "key_file_mode = 644",
        'key_file_mode = "0640"',
        "key_file_mode = 0640\nkey_file_mode = 0640",
    ],
)
def test_file_mode_parser_rejects_missing_malformed_and_duplicate_modes(
    config: str,
) -> None:
    with pytest.raises(AssertionError):
        _file_mode(config, "key_file_mode")


@pytest.mark.parametrize("values", [[], [_GKE_VALUES], [_HOME_VALUES]])
def test_default_and_checked_in_overlays_leave_the_dial_fully_off(
    values: list[Path],
) -> None:
    release = "cp-off"
    documents = _render(release, values=values)
    pod = _control_plane(documents, release)
    control_plane = _container(pod, "control-plane")
    env = _named(control_plane.get("env"))

    assert not (_TLS_ENV & set(env))
    assert "control-plane-spiffe-helper" not in _named(pod["containers"])
    assert "control-plane-svid" not in _named(control_plane.get("volumeMounts"))
    volumes = _named(pod.get("volumes"))
    assert "control-plane-svid" not in volumes
    assert "spiffe-workload-api" not in volumes
    assert _config_map(documents, f"{release}-embervm-spiffe-helper") is None


def test_all_checked_in_values_keep_both_flags_false() -> None:
    for path in [_CHART_DIR / "values.yaml", _GKE_VALUES, _HOME_VALUES]:
        values = yaml.safe_load(path.read_text())
        spiffe = values.get("controlPlane", {}).get("spiffe")
        if path == _CHART_DIR / "values.yaml":
            assert spiffe == {
                **spiffe,
                "enabled": False,
                "dialNoded": False,
            }, path
        else:
            # The overlays do not mention the block at all; the chart default
            # governs. A future overlay that sets it must set it false.
            assert spiffe is None or (
                spiffe.get("enabled") is False and spiffe.get("dialNoded") is False
            ), path


def test_sidecar_alone_delivers_files_but_keeps_the_plaintext_dial() -> None:
    release = "cp-sidecar"
    documents = _render(release, ["controlPlane.spiffe.enabled=true"])
    pod = _control_plane(documents, release)
    control_plane = _container(pod, "control-plane")
    helper = _container(pod, "control-plane-spiffe-helper")

    assert not (_TLS_ENV & set(_named(control_plane["env"])))

    assert helper["image"] == "ghcr.io/spiffe/spiffe-helper:0.11.0"
    assert helper["args"] == ["-config", "/etc/spiffe-helper/helper.conf"]
    assert helper["readinessProbe"]["httpGet"] == {
        "path": "/ready",
        "port": "spiffe-health",
    }
    assert _named(helper["ports"])["spiffe-health"]["containerPort"] == 8091
    assert helper["securityContext"]["readOnlyRootFilesystem"] is True
    assert helper["securityContext"]["allowPrivilegeEscalation"] is False
    assert helper["securityContext"]["capabilities"]["drop"] == ["ALL"]
    helper_uid = helper["securityContext"].get(
        "runAsUser", pod["securityContext"]["runAsUser"]
    )
    consumer_uid = control_plane["securityContext"].get(
        "runAsUser", pod["securityContext"]["runAsUser"]
    )
    assert helper_uid == consumer_uid == 65532
    assert pod["securityContext"]["fsGroup"] == 65532

    helper_mounts = _named(helper["volumeMounts"])
    assert helper_mounts["spiffe-workload-api"]["readOnly"] is True
    assert helper_mounts["control-plane-spiffe-helper-config"]["readOnly"] is True
    assert helper_mounts["control-plane-svid"]["mountPath"] == "/run/embervm-svid"
    assert "readOnly" not in helper_mounts["control-plane-svid"]

    cp_mount = _named(control_plane["volumeMounts"])["control-plane-svid"]
    assert cp_mount == {
        "name": "control-plane-svid",
        "mountPath": "/run/embervm-svid",
        "readOnly": True,
    }

    volumes = _named(pod["volumes"])
    assert volumes["spiffe-workload-api"]["csi"] == {
        "driver": "csi.spiffe.io",
        "readOnly": True,
    }
    assert volumes["control-plane-svid"]["emptyDir"] == {
        "medium": "Memory",
        "sizeLimit": "1Mi",
    }
    assert volumes["control-plane-spiffe-helper-config"]["configMap"] == {
        "name": f"{release}-embervm-spiffe-helper",
        "defaultMode": 0o444,
    }

    config_map = _config_map(documents, f"{release}-embervm-spiffe-helper")
    assert config_map is not None
    config = config_map["data"]["helper.conf"]
    assert 'agent_address = "/spiffe-workload-api/spire-agent.sock"' in config
    assert 'cert_dir = "/run/embervm-svid"' in config
    assert 'hint = "embervm-platform"' in config
    assert 'svid_file_name = "svid.pem"' in config
    assert 'svid_key_file_name = "svid_key.pem"' in config
    assert 'svid_bundle_file_name = "svid_bundle.pem"' in config
    cert_mode = _file_mode(config, "cert_file_mode")
    key_mode = _file_mode(config, "key_file_mode")
    assert cert_mode & 0o200, "certificate must remain owner-writable for rotation"
    assert key_mode & 0o200, "key must remain owner-writable for rotation"
    assert cert_mode & 0o400, "same-UID control plane must be able to read certificate"
    assert key_mode & 0o400, "same-UID control plane must be able to read key"
    assert key_mode & 0o007 == 0, "key must grant no permissions to other users"
    assert "bind_port = 8091" in config


def test_dial_renders_tls_env_that_matches_the_noded_listener_byte_for_byte() -> None:
    release = "cp-dial"
    documents = _render(
        release,
        [
            "controlPlane.spiffe.enabled=true",
            "controlPlane.spiffe.dialNoded=true",
            "controlPlane.spiffe.svidDir=/var/run/svid",
            "noded.spiffe.enabled=true",
            "noded.spiffe.grpcTlsPort=19443",
            "noded.bearerTokenSecret.enabled=true",
            "noded.bearerTokenSecret.name=noded-bearer",
        ],
    )
    pod = _control_plane(documents, release)
    control_plane = _container(pod, "control-plane")
    env = {
        name: item["value"]
        for name, item in _named(control_plane["env"]).items()
        if "value" in item
    }

    assert env["EMBERVM_NODED_DIAL_TLS"] == "true"
    assert env["EMBERVM_NODED_TLS_PORT"] == "19443"
    assert env["EMBERVM_NODED_SVID_CERT"] == "/var/run/svid/svid.pem"
    assert env["EMBERVM_NODED_SVID_KEY"] == "/var/run/svid/svid_key.pem"
    assert env["EMBERVM_NODED_SVID_BUNDLE"] == "/var/run/svid/svid_bundle.pem"
    assert (
        _named(control_plane["volumeMounts"])["control-plane-svid"]["mountPath"]
        == "/var/run/svid"
    )
    # Dual window: the bearer keeps flowing beside the mTLS dial.
    assert "EMBERVM_NODED_BEARER_TOKEN" in _named(control_plane["env"])

    # The seam with PR #6378, both directions:
    #  * the ID the control plane REQUIRES of noded is noded's own SA identity;
    #  * the ID noded's allowlist ACCEPTS is this control plane's SA identity;
    #  * the port the control plane dials is the port noded listens on.
    control_plane_sa = pod["serviceAccountName"]
    for noded_pod in _noded_pods(documents):
        noded = _container(noded_pod, "noded")
        noded_env = {
            name: item["value"]
            for name, item in _named(noded["env"]).items()
            if "value" in item
        }
        noded_sa = noded_pod["serviceAccountName"]
        assert env["EMBERVM_NODED_SPIFFE_ID"] == (
            f"spiffe://embervm.jomcgi.dev/ns/{release}/sa/{noded_sa}"
        )
        assert noded_env["EMBERVM_NODED_SPIFFE_CLIENT_IDS"] == (
            f"spiffe://embervm.jomcgi.dev/ns/{release}/sa/{control_plane_sa}"
        )
        assert (
            noded_env["EMBERVM_NODED_TLS_LISTEN_ADDR"]
            == f":{env['EMBERVM_NODED_TLS_PORT']}"
        )
        assert _named(noded["ports"])["grpc-tls"]["containerPort"] == 19443
        # The plaintext listener the bearer still reaches stays up.
        assert _named(noded["ports"])["grpc"]["containerPort"] == 9090


def test_dial_shares_the_trust_domain_the_noded_listener_uses() -> None:
    release = "cp-domain"
    documents = _render(
        release,
        [
            "controlPlane.spiffe.enabled=true",
            "controlPlane.spiffe.dialNoded=true",
            "noded.spiffe.enabled=true",
            "noded.spiffe.trustDomain=other.test",
        ],
    )
    control_plane = _container(_control_plane(documents, release), "control-plane")
    spiffe_id = _named(control_plane["env"])["EMBERVM_NODED_SPIFFE_ID"]["value"]
    assert spiffe_id.startswith("spiffe://other.test/ns/")


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            ["controlPlane.spiffe.dialNoded=true"],
            "dialNoded requires controlPlane.spiffe.enabled",
        ),
        (
            ["controlPlane.spiffe.enabled=true", "controlPlane.spiffe.dialNoded=true"],
            "dialNoded requires noded.spiffe.enabled",
        ),
    ],
)
def test_render_rejects_a_dial_without_its_sidecar_or_its_listener(
    settings: list[str], message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _render("cp-invalid", settings)
