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
    # The chart ships no Secret, so it offers nothing for egress injection;
    # kagent's database Secrets come from outside and stay unlabelled.
    assert not [d for d in docs if d["kind"] == "Secret"]


def test_rbac_is_namespaced() -> None:
    # Cluster-wide ClusterRoles would hand every namespace's Secrets to a
    # controller behind unverified trusted-proxy auth.
    docs = _render()
    cluster_scoped = {
        d["metadata"]["name"]
        for d in docs
        if d["kind"] in ("ClusterRole", "ClusterRoleBinding")
    }
    assert cluster_scoped == {"kagent-kagent-crds"}
    roles = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Role"}
    assert {r["metadata"]["namespace"] for r in roles.values()} == {"kagent"}
    assert "kagent-reconciliation-role" in roles
    config = yaml.safe_load(
        _one(docs, "ConfigMap", "kagent-controller")["data"]["controller.yaml"]
    )
    assert config["kubernetes"]["watchNamespaces"] == ["kagent"]


def test_identity_lands_with_the_namespace() -> None:
    sa = _one(_render(), "ServiceAccount", "kagent-controller")
    assert sa["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-2"


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


def _pod_labels(docs: list[dict[str, Any]], name: str) -> dict[str, str]:
    return _one(docs, "Deployment", name)["spec"]["template"]["metadata"]["labels"]


def test_policies_select_the_kagent_pods() -> None:
    docs = _render()
    for policy, deployment in (
        ("kagent-controller", "kagent-controller"),
        ("kagent-ui", "kagent-ui"),
    ):
        selector = _one(docs, "CiliumNetworkPolicy", policy)["spec"][
            "endpointSelector"
        ]["matchLabels"]
        assert selector == _pod_labels(docs, deployment)


def test_controller_admits_only_bearer_paths() -> None:
    docs = _render()
    spec = _one(docs, "CiliumNetworkPolicy", "kagent-controller")["spec"]
    ui = _pod_labels(docs, "kagent-ui")
    controller = _pod_labels(docs, "kagent-controller")
    assert spec["ingress"] == [
        {
            "fromEndpoints": [
                {"matchLabels": ui},
                {"matchLabels": controller},
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "edge",
                        "gateway.envoyproxy.io/owning-gateway-name": "edge",
                        "gateway.envoyproxy.io/owning-gateway-namespace": "edge",
                    }
                },
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "factory",
                        "app.kubernetes.io/name": "factory-connector",
                    }
                },
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "ate-system",
                        "app": "atenet-egress",
                    }
                },
            ],
            "toPorts": [{"ports": [{"port": "8083", "protocol": "TCP"}]}],
        }
    ]

    def to(selectors: list[dict[str, str]], port: str) -> dict[str, Any]:
        return {
            "toEndpoints": [{"matchLabels": m} for m in selectors],
            "toPorts": [{"ports": [{"port": port, "protocol": "TCP"}]}],
        }

    substrate = "ate-system"
    ns = "k8s:io.kubernetes.pod.namespace"
    assert spec["egress"] == [
        {"toEntities": ["kube-apiserver"]},
        to([controller], "8083"),
        to([{ns: substrate, "app": "ate-api-server"}], "443"),
        to(
            [
                {ns: substrate, "app": "ate-idp-server"},
                {ns: substrate, "app": "atenet-router"},
            ],
            "8443",
        ),
        to([{ns: "kagent-pg", "cnpg.io/cluster": "kagent-pg"}], "5432"),
    ]


def test_ui_admits_only_the_edge() -> None:
    docs = _render()
    spec = _one(docs, "CiliumNetworkPolicy", "kagent-ui")["spec"]
    assert spec["ingress"] == [
        {
            "fromEndpoints": [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "edge",
                        "gateway.envoyproxy.io/owning-gateway-name": "edge",
                        "gateway.envoyproxy.io/owning-gateway-namespace": "edge",
                    }
                }
            ],
            "toPorts": [{"ports": [{"port": "8080", "protocol": "TCP"}]}],
        }
    ]
    assert spec["egress"] == [
        {
            "toEndpoints": [{"matchLabels": _pod_labels(docs, "kagent-controller")}],
            "toPorts": [{"ports": [{"port": "8083", "protocol": "TCP"}]}],
        }
    ]


def test_existing_kagent_renders_only_crds() -> None:
    # Helm cannot make crds/ conditional. The factory Application sets
    # helm.skipCrds, so under ArgoCD an existing install renders nothing.
    docs = _render(["kagent.mode=existing", "kagent.enabled=false"])
    assert {d["kind"] for d in docs} == {"CustomResourceDefinition"}


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("kagent.enabled=false", "kagent.enabled must be true for kagent.mode install"),
        (
            "itsaplan.enabled=true",
            r"itsaplan\.enabled must be one of|itsaplan is not implemented",
        ),
        (
            "broker.enabled=true",
            r"broker\.enabled must be one of|broker is not implemented",
        ),
        (
            "connector.enabled=true",
            r"connector\.enabled must be one of|connector is not implemented",
        ),
        (
            "repositories[0].repo=homelab",
            r"repositories: Array must have at most 0|repositories, agents and providers",
        ),
        ("postgres.mode=install", "postgres.mode"),
    ],
)
# The schema rejects most of these before templates/validate.yaml does; the
# pattern accepts either layer's message.
def test_unimplemented_or_inconsistent_values_fail(override: str, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        _render([override])
