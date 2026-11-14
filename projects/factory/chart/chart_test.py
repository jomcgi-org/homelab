"""Pins for the factory chart: the CRDs in crds/ are the vendored kagent-crds
chart's, the controller's identity and namespace are kept, the namespace may
call Substrate, the controller admits only its bearer paths, the credential
broker holds every provider credential with only the access ARCHITECTURE.md
lists, each repository binding's workspace gets placeholders only, and later
steps of the delivery plan fail the render instead of doing nothing."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import tempfile
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


# The deploy values turn the homelab binding on; tests of the chart's
# defaults render with only their storage settings.
_DEFAULTS_FILE = Path(tempfile.mkdtemp()) / "defaults.yaml"
_DEFAULTS_FILE.write_text(
    yaml.safe_dump({"postgres": yaml.safe_load(_DEPLOY_VALUES.read_text())["postgres"]})
)


def _render(
    overrides: list[str] | None = None, binding: bool = True
) -> list[dict[str, Any]]:
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
    if not binding:
        argv[argv.index(str(_DEPLOY_VALUES))] = str(_DEFAULTS_FILE)
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


def test_credential_provider_reads_only_granted_namespaces() -> None:
    docs = _render()
    bindings = [
        d
        for d in docs
        if d["kind"] == "RoleBinding"
        and d["metadata"]["name"] == "substrate-credential-provider"
    ]
    assert {b["metadata"]["namespace"] for b in bindings} == {
        "kagent",
        "factory-r-homelab",
    }
    for binding in bindings:
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
    # The only Secret the chart ships is the broker's empty grant, outside
    # every granted namespace; nothing it renders is injectable.
    # itsaplan's Secrets (itsaplan_test.py) are in factory too, also empty.
    secrets = [
        d
        for d in docs
        if d["kind"] == "Secret"
        and not d["metadata"]["name"].startswith("factory-itsaplan")
    ]
    assert [(d["metadata"]["namespace"], d["metadata"]["name"]) for d in secrets] == [
        ("factory", "factory-broker-grant-codex-primary")
    ]
    assert "data" not in secrets[0] and "stringData" not in secrets[0]
    assert not [
        d
        for d in docs
        if d.get("metadata", {}).get("labels", {}).get("substrate.ate.dev/injectable")
    ]


def test_rbac_is_namespaced() -> None:
    # Cluster-wide ClusterRoles would hand every namespace's Secrets to a
    # controller behind unverified trusted-proxy auth.
    docs = _render()
    cluster_scoped = {
        d["metadata"]["name"]: d
        for d in docs
        if d["kind"] in ("ClusterRole", "ClusterRoleBinding")
    }
    assert set(cluster_scoped) == {
        "kagent-kagent-crds",
        "factory-broker-tokenreview",
        "kagent-kagent-invocation-auth",
        # The controller's secure metrics (deploy values): it reviews a
        # scraper's token and access to GET /metrics, nothing else.
        "kagent-metrics-auth-role",
        "kagent-metrics-auth-rolebinding",
        "kagent-metrics-reader",
    }
    assert _one(docs, "ClusterRole", "kagent-metrics-auth-role")["rules"] == [
        {
            "apiGroups": ["authentication.k8s.io"],
            "resources": ["tokenreviews"],
            "verbs": ["create"],
        },
        {
            "apiGroups": ["authorization.k8s.io"],
            "resources": ["subjectaccessreviews"],
            "verbs": ["create"],
        },
    ]
    assert _one(docs, "ClusterRole", "kagent-metrics-reader")["rules"] == [
        {"nonResourceURLs": ["/metrics"], "verbs": ["get"]}
    ]
    # kagent reviews the connector's ServiceAccount token, and nothing else.
    assert _one(docs, "ClusterRole", "kagent-kagent-invocation-auth")["rules"] == [
        {
            "apiGroups": ["authentication.k8s.io"],
            "resources": ["tokenreviews"],
            "verbs": ["create"],
        }
    ]
    kagent_roles = {
        (d["metadata"]["namespace"], d["metadata"]["name"])
        for d in docs
        if d["kind"] == "Role" and d["metadata"]["name"].startswith("kagent-")
    }
    assert {ns for ns, _ in kagent_roles} == {"kagent", "factory-r-homelab"}
    assert ("factory-r-homelab", "kagent-reconciliation-role") in kagent_roles
    config = yaml.safe_load(
        _one(docs, "ConfigMap", "kagent-controller")["data"]["controller.yaml"]
    )
    assert sorted(config["kubernetes"]["watchNamespaces"]) == [
        "factory-r-homelab",
        "kagent",
    ]


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
    # The only ModelConfigs are the bindings' relay configs.
    assert {d["metadata"]["namespace"] for d in docs if d["kind"] == "ModelConfig"} == {
        "factory-r-homelab"
    }
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
    docs = _render(["kagent.mode=existing", "kagent.enabled=false"], binding=False)
    assert {d["kind"] for d in docs} == {"CustomResourceDefinition"}


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("kagent.enabled=false", "kagent.enabled must be true for kagent.mode install"),
        ("itsaplan.enabled=false", r"connector\.enabled needs itsaplan\.enabled"),
        (
            "kagent.config.server.auth.serviceAccount.authority=",
            r"connector\.enabled needs kagent\.config\.server\.auth\.serviceAccount\.authority",
        ),
        (
            "providers.openai.kind=x",
            r"providers: Must have at most 0 properties|providers are not implemented",
        ),
        ("repositories[0].agent=missing", 'agent "missing" is not in agents'),
        ("repositories[0].namespace=kagent", "not a binding namespace"),
        ("kagent.rbac.namespaces={kagent}", "add namespace factory-r-homelab"),
        ("broker.enabled=false", "needs broker.enabled"),
        (
            "kagent.config.runtimeDefaults.cpu=2",
            "must equal kagent.config.runtimeDefaults",
        ),
        ("agents.codex.provider=openai", r"agents\.codex\.provider"),
        ("postgres.mode=install", "postgres.mode"),
    ],
)
# The schema rejects some of these before templates/validate.yaml does; the
# pattern accepts either layer's message.
def test_unimplemented_or_inconsistent_values_fail(override: str, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        _render([override])


def _in(docs: list[dict[str, Any]], kind: str, ns: str, name: str) -> dict[str, Any]:
    found = [
        d
        for d in docs
        if d["kind"] == kind
        and d["metadata"]["name"] == name
        and d["metadata"].get("namespace") == ns
    ]
    assert len(found) == 1, f"expected one {kind} {ns}/{name}, found {len(found)}"
    return found[0]


def test_broker_runs_one_replica_from_its_config() -> None:
    docs = _render(
        ["broker.image.digest=sha256:" + "a" * 64, "broker.githubApp.enabled=false"]
    )
    deploy = _in(docs, "Deployment", "factory", "factory-broker")
    assert deploy["spec"]["replicas"] == 1
    assert deploy["spec"]["strategy"] == {"type": "Recreate"}
    pod = deploy["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "factory-broker"
    (container,) = pod["containers"]
    assert container["image"] == (
        "ghcr.io/jomcgi/homelab/projects/factory/broker@sha256:" + "a" * 64
    )
    # No GitHub App yet: no key volume, no 1Password item, minting off.
    assert "github-app" not in {v["name"] for v in pod["volumes"]}
    assert not [
        d
        for d in docs
        if d["kind"] == "OnePasswordItem"
        and d["metadata"]["name"] == "factory-github-app"
    ]
    config = yaml.safe_load(
        _in(docs, "ConfigMap", "factory", "factory-broker")["data"]["config.yaml"]
    )
    assert "github" not in config
    assert config["listenAddr"] == ":8080" and config["adminListenAddr"] == ":8081"
    assert config["actorJwt"] == {
        "issuer": "https://idp.ate-system.svc",
        "audience": "factory-broker",
        "caFile": "/run/substrate-servicedns/trust-bundle.pem",
        "allowedAtespaces": ["factory-r-homelab"],
    }
    assert config["bindings"] == [
        {
            "name": "homelab",
            "repository": "jomcgi-org/homelab",
            "namespace": "factory-r-homelab",
        }
    ]


def test_github_app_is_optional() -> None:
    docs = _render(
        ["broker.enabled=true", "broker.githubApp.enabled=true"], binding=False
    )
    item = _in(docs, "OnePasswordItem", "factory", "factory-github-app")
    assert item["spec"]["itemPath"] == "vaults/k8s-homelab/items/factory-github-app"
    # The deploy values read the homelab App from its own item.
    item = _in(_render(), "OnePasswordItem", "factory", "factory-github-app")
    assert item["spec"]["itemPath"] == "vaults/k8s-homelab/items/factory-gh-app"
    docs = _render(["broker.githubApp.enabled=true"])
    pod = _in(docs, "Deployment", "factory", "factory-broker")["spec"]["template"]
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    assert volumes["github-app"]["secret"]["secretName"] == "factory-github-app"
    config = yaml.safe_load(
        _in(docs, "ConfigMap", "factory", "factory-broker")["data"]["config.yaml"]
    )
    assert config["github"]["privateKeyFile"] == "/etc/factory-github-app/private-key"


def test_broker_state_is_kept_and_pre_created() -> None:
    docs = _render()
    for kind, name in (
        ("Secret", "factory-broker-grant-codex-primary"),
        ("ConfigMap", "factory-broker-quota"),
        ("Namespace", "factory"),
    ):
        obj = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name][
            0
        ]
        assert _KEEP.items() <= obj["metadata"]["annotations"].items()


def test_broker_rbac_is_by_name() -> None:
    docs = _render()
    own = _in(docs, "Role", "factory", "factory-broker")
    assert own["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["factory-broker-grant-codex-primary"],
            "verbs": ["get", "update"],
        },
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "resourceNames": ["factory-broker-quota"],
            "verbs": ["get", "update"],
        },
    ]
    binding = _in(docs, "Role", "factory-r-homelab", "factory-broker")
    assert binding["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["github-git"],
            "verbs": ["get", "update"],
        },
    ]
    tokenreview = _one(docs, "ClusterRole", "factory-broker-tokenreview")
    assert tokenreview["rules"] == [
        {
            "apiGroups": ["authentication.k8s.io"],
            "resources": ["tokenreviews"],
            "verbs": ["create"],
        }
    ]


def test_broker_policy() -> None:
    docs = _render()
    spec = _in(docs, "CiliumNetworkPolicy", "factory", "factory-broker")["spec"]
    pod = _in(docs, "Deployment", "factory", "factory-broker")["spec"]["template"]
    assert (
        spec["endpointSelector"]["matchLabels"].items()
        <= pod["metadata"]["labels"].items()
    )
    ns = "k8s:io.kubernetes.pod.namespace"
    assert spec["ingress"] == [
        {
            "fromEndpoints": [
                {"matchLabels": {ns: "ate-system", "app": "atenet-egress"}}
            ],
            "toPorts": [{"ports": [{"port": "8080", "protocol": "TCP"}]}],
        },
        {
            "fromEndpoints": [
                {
                    "matchLabels": {
                        ns: "factory",
                        "app.kubernetes.io/name": "factory-connector",
                    }
                }
            ],
            "toPorts": [{"ports": [{"port": "8081", "protocol": "TCP"}]}],
        },
    ]
    assert spec["egress"] == [
        {"toEntities": ["kube-apiserver"]},
        {
            "toEndpoints": [
                {"matchLabels": {ns: "ate-system", "app": "ate-idp-server"}}
            ],
            "toPorts": [{"ports": [{"port": "8443", "protocol": "TCP"}]}],
        },
        {
            "toFQDNs": [
                {"matchName": "auth.openai.com"},
                {"matchName": "chatgpt.com"},
                {"matchName": "api.github.com"},
            ],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        },
    ]


def test_binding_relays_through_the_broker_with_an_actor_jwt() -> None:
    docs = _render()
    ns = "factory-r-homelab"
    assert _in(docs, "ModelConfig", ns, "codex")["spec"] == {
        "provider": "OpenAI",
        "model": "gpt-6-astra",
        "openAI": {
            "baseUrl": "http://factory-broker.factory.svc.cluster.local/v1",
            "apiFormat": "responses",
        },
        "actorJwt": {"audiences": ["factory-broker"]},
    }
    agent = _in(docs, "Agent", ns, "homelab-codex")["spec"]
    assert agent["harnessRef"] == {"name": "codex"}
    assert agent["template"]["inline"]["modelConfig"] == {"name": "codex"}
    assert agent["egressPolicyRefs"] == [{"name": "github-git"}]
    harness = _in(docs, "Harness", ns, "codex")["spec"]
    assert harness["codex"] == {}
    assert harness["substrate"] == {
        "workerPoolRef": {"name": "homelab-codex"},
        "snapshotPolicy": {"location": "s3://ate-snapshots/factory-r-homelab/"},
    }


def test_binding_workspace_holds_only_placeholders() -> None:
    docs = _render()
    ns = "factory-r-homelab"
    env = {
        e["name"]: e["value"] for e in _in(docs, "Harness", ns, "codex")["spec"]["env"]
    }
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Basic factory-placeholder"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert _in(docs, "EgressPolicy", ns, "github-git")["spec"] == {
        "destinations": [
            {
                "origin": "https://github.com",
                "authentication": {
                    "header": "Authorization",
                    "prefix": "Basic ",
                    "secretKeyRef": {"name": "github-git", "key": "basic"},
                },
            }
        ]
    }
    sa = _in(docs, "ServiceAccount", ns, "homelab-codex-worker")
    assert sa["automountServiceAccountToken"] is False


def test_binding_worker_pool_sets_resources_and_concurrency() -> None:
    docs = _render()
    pool = _in(docs, "WorkerPool", "factory-r-homelab", "homelab-codex")
    labels = pool["metadata"]["labels"]
    assert labels["kagent.dev/worker-pool-name"] == "homelab-codex"
    assert labels["kagent.dev/worker-pool-namespace"] == "factory-r-homelab"
    assert pool["spec"]["replicas"] == 1
    assert pool["spec"]["template"]["serviceAccountName"] == "homelab-codex-worker"
    # The worker's capacity is its limit less a 64Mi reserve, and must hold
    # the actor's 8Gi plus gVisor's sentry.
    assert pool["spec"]["template"]["resources"]["limits"] == {
        "cpu": "4",
        "memory": "9Gi",
    }
    assert pool["spec"]["sandboxClasses"] == [
        {"name": "gvisor", "configRef": {"name": "gvisor-default"}}
    ]
    config = yaml.safe_load(
        _one(docs, "ConfigMap", "kagent-controller")["data"]["controller.yaml"]
    )
    assert config["runtimeDefaults"]["cpu"] == "4"
    assert config["runtimeDefaults"]["memory"] == "8Gi"


def test_binding_namespace_policy_admits_only_substrate() -> None:
    docs = _render()
    spec = _in(docs, "CiliumNetworkPolicy", "factory-r-homelab", "substrate-workers")[
        "spec"
    ]
    ns = "k8s:io.kubernetes.pod.namespace"
    assert spec["ingress"] == [
        {
            "fromEndpoints": [
                {"matchLabels": {ns: "ate-system", "app": "atenet-router"}}
            ],
            "toPorts": [{"ports": [{"port": "8443", "protocol": "TCP"}]}],
        }
    ]
    assert spec["egress"] == [
        {
            "toEndpoints": [
                {"matchLabels": {ns: "ate-system", "app": "atenet-egress"}}
            ],
            "toPorts": [{"ports": [{"port": "8443", "protocol": "TCP"}]}],
        }
    ]


def test_deploy_values_render_no_broker_or_binding() -> None:
    # The chart's defaults render nothing beyond kagent, so an install turns
    # the broker and its bindings on explicitly.
    docs = _render(binding=False)
    namespaces = {d["metadata"].get("namespace") for d in docs}
    assert namespaces <= {"kagent", None}
    assert not [
        d
        for d in docs
        if d["kind"]
        in (
            "Agent",
            "ModelConfig",
            "WorkerPool",
            "OnePasswordItem",
            "ValidatingAdmissionPolicy",
        )
        or d["metadata"]["name"].startswith("factory-broker")
    ]


def test_broker_pulls_with_the_ghcr_secret() -> None:
    docs = _render()
    item = _in(docs, "OnePasswordItem", "factory", "ghcr-imagepull-secret")
    assert item["type"] == "kubernetes.io/dockerconfigjson"
    assert item["spec"]["itemPath"] == "vaults/k8s-homelab/items/ghcr-read-permissions"
    pod = _in(docs, "Deployment", "factory", "factory-broker")["spec"]["template"]
    assert pod["spec"]["imagePullSecrets"] == [{"name": "ghcr-imagepull-secret"}]


def test_broker_secret_writes_are_confined() -> None:
    docs = _render()
    policy = _one(docs, "ValidatingAdmissionPolicy", "factory-broker-secrets")["spec"]
    assert policy["matchConditions"] == [
        {
            "name": "broker",
            "expression": "request.userInfo.username == "
            "'system:serviceaccount:factory:factory-broker'",
        }
    ]
    (validation,) = policy["validations"]
    for fragment in (
        "'factory-broker-grant-codex-primary'",
        "object.metadata.name == 'github-git'",
        "object.type == 'Opaque'",
    ):
        assert fragment in validation["expression"]
    binding = _one(docs, "ValidatingAdmissionPolicyBinding", "factory-broker-secrets")
    assert binding["spec"] == {
        "policyName": "factory-broker-secrets",
        "validationActions": ["Deny"],
    }


def test_operator_service_account_is_optional() -> None:
    def admin_subjects(docs: list[dict[str, Any]]) -> list[str]:
        cm = _in(docs, "ConfigMap", "factory", "factory-broker")
        return yaml.safe_load(cm["data"]["config.yaml"])["tokenReview"]["adminSubjects"]

    docs = _render(["broker.operatorServiceAccount="])
    assert admin_subjects(docs) == []
    assert not [
        d for d in docs if d["metadata"]["name"] == "factory-broker-operator-pods"
    ]

    subject = "system:serviceaccount:factory:factory-operator"
    docs = _render(
        [
            "broker.operatorServiceAccount=factory-operator",
            f"broker.adminSubjects={{{subject}}}",
        ]
    )
    # Listed once, though also named in adminSubjects.
    assert admin_subjects(docs) == [subject]
    sa = _in(docs, "ServiceAccount", "factory", "factory-operator")
    assert sa["automountServiceAccountToken"] is False
    policy = _one(docs, "ValidatingAdmissionPolicy", "factory-broker-operator-pods")
    assert policy["spec"]["matchConstraints"]["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": "factory"}
    }
    (validation,) = policy["spec"]["validations"]
    assert "!= 'factory-operator'" in validation["expression"]
    binding = _one(
        docs, "ValidatingAdmissionPolicyBinding", "factory-broker-operator-pods"
    )
    assert binding["spec"] == {
        "policyName": "factory-broker-operator-pods",
        "validationActions": ["Deny"],
    }
    # Pods are refused before the ServiceAccount exists.
    for d in (policy, binding):
        assert d["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-3"


@pytest.mark.parametrize(
    "name",
    ["default", "factory-broker", "factory-connector", "factory-itsaplan-bootstrap"],
)
def test_operator_service_account_is_not_a_chart_pod_identity(name: str) -> None:
    with pytest.raises(RuntimeError, match="ServiceAccount the chart's Pods use"):
        _render([f"broker.operatorServiceAccount={name}"])


def test_binding_reader_follows_the_credential_provider_switch() -> None:
    docs = _render(["substrate.credentialProvider.secretReaderBinding=false"])
    assert not [
        d
        for d in docs
        if d["kind"] == "RoleBinding"
        and d["metadata"]["name"] == "substrate-credential-provider"
    ]
