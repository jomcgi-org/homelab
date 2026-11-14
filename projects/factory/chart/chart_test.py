"""Pins for the factory chart's kagent install: the CRDs in crds/ are the
vendored kagent-crds chart's, the controller's identity and namespace are kept,
the namespace may call Substrate, the controller admits only its bearer paths,
and later steps of the delivery plan fail the render instead of doing nothing."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parent
_DEPLOY_VALUES = Path(
    os.environ.get("DEPLOY_VALUES", _CHART_DIR.parent / "deploy" / "values.yaml")
)
_KEEP = {
    "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
    "helm.sh/resource-policy": "keep",
}


def _render(overrides: list[str] | None = None) -> list[dict[str, Any]]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "factory",
        str(_CHART_DIR),
        "--namespace",
        "kagent",
        "--include-crds",
        "--values",
        str(_DEPLOY_VALUES),
    ]
    for override in overrides or []:
        argv += ["--set", override]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"expected one {kind}/{name}, found {len(found)}"
    return found[0]


def test_crds_match_vendored_chart() -> None:
    with tarfile.open(_CHART_DIR / "charts" / "kagent-crds-1.0.1.tgz") as tgz:
        vendored = {
            Path(m.name).name: tgz.extractfile(m).read()  # type: ignore[union-attr]
            for m in tgz.getmembers()
            if m.isfile() and "/templates/" in m.name
        }
    local = {p.name: p.read_bytes() for p in (_CHART_DIR / "crds").glob("*.yaml")}
    assert local == vendored


def test_crd_hook_applies_every_crd() -> None:
    docs = _render()
    crds = {
        d["metadata"]["name"] for d in docs if d["kind"] == "CustomResourceDefinition"
    }
    assert len(crds) == 8
    config = _one(docs, "ConfigMap", "kagent-crds")
    assert {
        yaml.safe_load(io.StringIO(body))["metadata"]["name"]
        for body in config["data"].values()
    } == crds
    role = _one(docs, "ClusterRole", "kagent-kagent-crds")
    scoped = [r for r in role["rules"] if "resourceNames" in r]
    assert set(scoped[0]["resourceNames"]) == crds
    job = _one(docs, "Job", "kagent-crds")
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert "--server-side" in job["spec"]["template"]["spec"]["containers"][0]["args"]


def test_namespace_can_call_substrate_and_is_kept() -> None:
    ns = _one(_render(), "Namespace", "kagent")
    assert ns["metadata"]["labels"]["ate.dev/api-access"] == "true"
    assert _KEEP.items() <= ns["metadata"]["annotations"].items()


def test_controller_identity_is_kept() -> None:
    # Substrate grants spiffe://cluster.local/ns/kagent/sa/kagent-controller,
    # and kagent principals include the ServiceAccount UID.
    docs = _render()
    sa = _one(docs, "ServiceAccount", "kagent-controller")
    assert sa["metadata"]["namespace"] == "kagent"
    assert _KEEP.items() <= sa["metadata"]["annotations"].items()
    deploy = _one(docs, "Deployment", "kagent-controller")
    assert (
        deploy["spec"]["template"]["spec"]["serviceAccountName"] == "kagent-controller"
    )


def test_credential_provider_reads_only_this_namespace() -> None:
    docs = _render()
    binding = _one(docs, "RoleBinding", "substrate-credential-provider")
    assert binding["metadata"]["namespace"] == "kagent"
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "k8s-credential-provider-secret-reader",
    }
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "k8s-credential-provider",
            "namespace": "ate-system",
        }
    ]
    # No Secret in the chart is offered for egress injection.
    for doc in docs:
        if doc["kind"] == "Secret":
            assert "substrate.ate.dev/injectable" not in doc["metadata"].get(
                "labels", {}
            )


def test_trusted_proxy_without_tool_server_or_default_model() -> None:
    docs = _render()
    config = yaml.safe_load(
        _one(docs, "ConfigMap", "kagent-controller")["data"]["controller.yaml"]
    )
    assert config["server"]["auth"]["mode"] == "trusted-proxy"
    assert config["substrate"]["clientCertFile"]
    kinds = {d["kind"] for d in docs}
    assert "ModelConfig" not in kinds
    assert not [
        d
        for d in docs
        if d["kind"] == "Deployment" and "tools" in d["metadata"]["name"]
    ]


def test_controller_admits_only_bearer_paths() -> None:
    policy = _one(_render(), "CiliumNetworkPolicy", "kagent-controller")
    peers = policy["spec"]["ingress"][0]["fromEndpoints"]
    namespaces = {
        p["matchLabels"].get("k8s:io.kubernetes.pod.namespace") for p in peers
    }
    # None: the UI and the controller itself, in the policy's namespace.
    assert namespaces == {None, "edge", "factory", "ate-system"}
    assert len(policy["spec"]["ingress"]) == 1
    egress_ns = {
        sel["matchLabels"].get("k8s:io.kubernetes.pod.namespace")
        for rule in policy["spec"]["egress"]
        for sel in rule.get("toEndpoints", [])
    }
    assert "ate-system" in egress_ns
    assert any(
        sel.get("matchLabels") == {"cnpg.io/cluster": "kagent-pg"}
        for rule in policy["spec"]["egress"]
        for sel in rule.get("toEndpoints", [])
    )


def test_existing_kagent_renders_only_crds() -> None:
    # Helm cannot make crds/ conditional; an existing install skips them with
    # the Application's helm.skipCrds.
    docs = _render(["kagent.mode=existing", "kagent.enabled=false"])
    assert {d["kind"] for d in docs} == {"CustomResourceDefinition"}


@pytest.mark.parametrize(
    "override",
    [
        "kagent.enabled=false",
        "itsaplan.enabled=true",
        "broker.enabled=true",
        "connector.enabled=true",
        "repositories[0].repo=homelab",
        "postgres.mode=install",
    ],
)
def test_unimplemented_or_inconsistent_values_fail(override: str) -> None:
    with pytest.raises(RuntimeError):
        _render([override])
