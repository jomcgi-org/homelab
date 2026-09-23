"""Render checks for the token broker's optional SPIFFE mTLS listener."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def _chart_dir() -> Path:
    chart = Path(__file__).resolve().parent
    if not (chart / "Chart.yaml").exists():
        raise RuntimeError("Could not find chart Chart.yaml")
    return chart


def _render(
    release: str,
    settings: list[str] | None = None,
    values: Path | None = None,
) -> str:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    argv = [
        helm_bin,
        "template",
        release,
        str(_chart_dir()),
        "--namespace",
        release,
    ]
    if values is not None:
        argv += ["--values", str(values)]
    for setting in settings or []:
        argv += ["--set", setting]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return result.stdout


def _source_document(rendered: str, template: str) -> str:
    marker = f"# Source: embervm/templates/{template}"
    documents = [document for document in rendered.split("\n---") if marker in document]
    assert len(documents) == 1, f"expected one document for {template}"
    return documents[0]


def test_spiffe_flag_off_omits_listener_port_and_csi_volume() -> None:
    rendered = _render("spiffe-off")
    deployment = _source_document(rendered, "tokenbroker-deployment.yaml")
    service = _source_document(rendered, "tokenbroker-service.yaml")

    assert "BROKER_TLS_LISTEN_ADDR" not in deployment
    assert "BROKER_SPIFFE_CLIENT_IDS" not in deployment
    assert "name: https" not in deployment
    assert "spiffe-workload-api" not in deployment
    assert "name: https" not in service
    assert (
        '- { name: BROKER_RETIRE_PLAINTEXT_PROTECTED_ROUTES, value: "false" }'
        in deployment
    )


def test_spiffe_flag_on_renders_default_noded_identity_and_tls_port() -> None:
    rendered = _render("broker-test", ["tokenBroker.spiffe.enabled=true"])
    deployment = _source_document(rendered, "tokenbroker-deployment.yaml")
    service = _source_document(rendered, "tokenbroker-service.yaml")

    assert '- { name: BROKER_TLS_LISTEN_ADDR, value: ":8443" }' in deployment
    assert (
        "- { name: BROKER_SPIFFE_CLIENT_IDS, value: "
        '"spiffe://embervm.jomcgi.dev/ns/broker-test/sa/broker-test-embervm-noded" }'
        in deployment
    )
    assert "- { name: https, containerPort: 8443 }" in deployment
    assert "name: spiffe-workload-api" in deployment
    assert "driver: csi.spiffe.io" in deployment
    assert "mountPath: /spiffe-workload-api" in deployment
    assert "- { name: https, port: 8443, targetPort: https }" in service


def test_spiffe_client_ids_render_as_comma_separated_env_value() -> None:
    rendered = _render(
        "spiffe-clients",
        [
            "tokenBroker.spiffe.enabled=true",
            "tokenBroker.spiffe.clientSpiffeIds={a,b}",
        ],
    )
    deployment = _source_document(rendered, "tokenbroker-deployment.yaml")

    assert '- { name: BROKER_SPIFFE_CLIENT_IDS, value: "a,b" }' in deployment


def test_plaintext_retirement_is_explicit_and_requires_mtls() -> None:
    rendered = _render(
        "retirement-on",
        [
            "tokenBroker.spiffe.enabled=true",
            "tokenBroker.plaintext.retireProtectedRoutes=true",
        ],
    )
    deployment = _source_document(rendered, "tokenbroker-deployment.yaml")
    assert (
        '- { name: BROKER_RETIRE_PLAINTEXT_PROTECTED_ROUTES, value: "true" }'
        in deployment
    )

    try:
        _render(
            "retirement-no-tls",
            ["tokenBroker.plaintext.retireProtectedRoutes=true"],
        )
    except RuntimeError as error:
        assert "retireProtectedRoutes requires tokenBroker.spiffe.enabled" in str(error)
    else:
        raise AssertionError("accepted plaintext retirement without mTLS")


def test_production_overlay_stages_allowlist_but_keeps_both_gates_off() -> None:
    defaults = yaml.safe_load((_chart_dir() / "values.yaml").read_text())
    gke = yaml.safe_load((_chart_dir().parent / "deploy/values-gke.yaml").read_text())

    assert defaults["tokenBroker"]["spiffe"]["enabled"] is False
    assert defaults["tokenBroker"]["plaintext"]["retireProtectedRoutes"] is False
    assert gke["tokenBroker"]["spiffe"]["enabled"] is False
    assert gke["tokenBroker"]["plaintext"]["retireProtectedRoutes"] is False
    assert gke["tokenBroker"]["spiffe"]["clientSpiffeIds"] == [
        "spiffe://embervm.jomcgi.dev/ns/embervm/sa/embervm-embervm-noded",
        "spiffe://embervm.jomcgi.dev/ns/monolith/sa/monolith",
    ]

    rendered = _render(
        "embervm",
        ["tokenBroker.spiffe.enabled=true"],
        _chart_dir().parent / "deploy/values-gke.yaml",
    )
    deployment = _source_document(rendered, "tokenbroker-deployment.yaml")
    assert (
        "spiffe://embervm.jomcgi.dev/ns/embervm/sa/embervm-embervm-noded,"
        "spiffe://embervm.jomcgi.dev/ns/monolith/sa/monolith"
    ) in deployment
    assert (
        '- { name: BROKER_RETIRE_PLAINTEXT_PROTECTED_ROUTES, value: "false" }'
        in deployment
    )


def _egress_settings() -> list[str]:
    return [
        "egress.enabled=true",
        "egress.secrets[0].header=Authorization",
        "egress.secrets[0].brokerGrant=codex-cluster",
        "egress.secrets[0].egressTo[0]=chatgpt.com",
    ]


def test_egress_defaults_preserve_plaintext_without_csi_mount() -> None:
    rendered = _render("client-off", _egress_settings())
    noded = _source_document(rendered, "noded-deployment.yaml")
    assert (
        'value: "client-off-embervm-tokenbroker.client-off.svc.cluster.local:8080"'
        in noded
    )
    assert "EGRESS_TOKEN_BROKER_SPIFFE_ID" not in noded
    assert "spiffe-workload-api" not in noded


def test_egress_mtls_wires_daemonset_and_bricks_with_exact_broker_identity() -> None:
    rendered = _render(
        "client-on",
        _egress_settings()
        + [
            "tokenBroker.spiffe.enabled=true",
            "tokenBroker.spiffe.tlsPort=9443",
            "tokenBroker.spiffe.trustDomain=custom.example",
            "egress.tokenBroker.spiffe.enabled=true",
            "egress.ca.enabled=true",
            "bricks.enabled=true",
        ],
    )
    nodes = [
        doc
        for doc in rendered.split("\n---")
        if (
            "# Source: embervm/templates/noded-deployment.yaml" in doc
            or "# Source: embervm/templates/brick-deployment.yaml" in doc
        )
        and ("\nkind: Deployment" in doc or "\nkind: DaemonSet" in doc)
    ]
    assert len(nodes) >= 2
    for node in nodes:
        sidecar = node.split("- name: egress-proxy", 1)[1]
        assert (
            'value: "https://client-on-embervm-tokenbroker.client-on.svc:9443"'
            in sidecar
        )
        assert (
            'value: "spiffe://custom.example/ns/client-on/sa/client-on-embervm-tokenbroker"'
            in sidecar
        )
        assert "EGRESS_TOKEN_BROKER_SPIFFE_ID" in sidecar
        assert "unix:///spiffe-workload-api/spire-agent.sock" in sidecar
        assert "mountPath: /spiffe-workload-api" in sidecar
        assert "mountPath: /etc/egress-ca" in sidecar
        assert "driver: csi.spiffe.io" in sidecar
        assert "GITHUB_APP_PRIVATE_KEY" not in node


def test_egress_mtls_requires_broker_listener() -> None:
    try:
        _render(
            "missing-listener",
            _egress_settings() + ["egress.tokenBroker.spiffe.enabled=true"],
        )
    except RuntimeError as error:
        assert "egress.tokenBroker.spiffe requires" in str(error)
    else:
        raise AssertionError("accepted an mTLS client without a broker listener")
