"""Guard the shape of the monolith API ingress allowlist (issue #4660).

templates/cilium-ingress-policy.yaml is an `ingress:` CiliumNetworkPolicy, so
every source it does not name is default-denied and every source it DOES name
can forge the X-Auth-Email identity header. Its failure mode for a forgotten
caller is a silent dial timeout, not a readable deny (PR #4294), which makes
the allowlist itself load-bearing in one direction only: it must stay exactly
as wide as the legitimate caller set, never wider.

#4660 narrowed the last two namespace-wide grants to specific pod identities:

  - envoy-gateway-system: only the envoy proxy pods Envoy Gateway manages
    (`app.kubernetes.io/name: envoy`, `app.kubernetes.io/component: proxy`).
    The namespace also holds four cloudflared pods whose only legitimate origin
    is the envoy service on :80; a direct cloudflared-to-monolith path would
    bypass both HTTPRoute routing and the gateway-wide ClientTrafficPolicy that
    strips inbound X-Auth-Email at the listener.
  - mcp: only Context Forge's gateway Deployment
    (`app: context-forge-gateway-mcp-stack-mcpgateway`), not CF's redis,
    postgres or cron jobs.

The cheap way for either narrowing to regress is someone "fixing" a dial
timeout by re-widening a selector back to the bare namespace. That diff reads
as harmless cleanup and silently reopens header forgery to everything in the
namespace, which is what this file makes fail.

The embervm namespace entries are namespace-wide ON PURPOSE (the guest egress
proxy sidecar runs on brick pods whose class labels are not part of the grant's
story; ADR 051 and ADR 035 section 4 record the accepted cost). The invariant
below therefore allows ns-only selectors for embervm and nowhere else.

These renders need helm. Under Bazel the env vars come from the BUILD target;
locally they fall back to sibling paths and `helm` from PATH.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

_NS_KEY = "io.kubernetes.pod.namespace"


def _chart_dir() -> Path:
    here = Path(__file__).resolve().parent
    if (here / "Chart.yaml").exists():
        return here
    raise RuntimeError("Could not find chart Chart.yaml")


def _env(name: str, fallback: Path) -> str:
    return os.environ.get(name) or str(fallback)


def _deploy_values() -> Path:
    return Path(_env("DEPLOY_VALUES", _chart_dir().parent / "deploy" / "values.yaml"))


def _dev_values() -> Path:
    return Path(
        _env("DEV_VALUES", _chart_dir().parent / "dev" / "deploy" / "values.yaml")
    )


def _release_name(application_yaml: Path) -> str:
    """Read releaseName from the Application, not from this test."""
    text = application_yaml.read_text()
    match = re.search(r"^\s*releaseName:\s*(\S+)\s*$", text, re.M)
    assert match, f"no releaseName found in {application_yaml}"
    return match.group(1)


def _render(release: str, values: list[Path]) -> str:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        release,
        str(_chart_dir()),
        "--namespace",
        release,
    ]
    for v in values:
        argv += ["--values", str(v)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("helm template timed out after 120s") from e
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return result.stdout


def _docs(rendered: str):
    for chunk in re.split(r"^---\s*$", rendered, flags=re.MULTILINE):
        if "kind: CiliumNetworkPolicy" not in chunk:
            continue
        doc = yaml.safe_load(chunk)
        if isinstance(doc, dict):
            yield doc


def _api_ingress_policy(rendered: str, release: str) -> dict:
    """The {{ .Release.Name }}-api-ingress CNP, loaded."""
    policies = [
        doc
        for doc in _docs(rendered)
        if doc.get("metadata", {}).get("name") == f"{release}-api-ingress"
    ]
    assert len(policies) == 1, (
        f"expected exactly one {release}-api-ingress CiliumNetworkPolicy, "
        f"found {len(policies)}"
    )
    return policies[0]


@pytest.fixture(scope="module")
def prod():
    chart = _chart_dir()
    release = _release_name(
        Path(_env("PROD_APPLICATION", chart.parent / "deploy" / "application.yaml"))
    )
    rendered = _render(release, [chart / "values.yaml", _deploy_values()])
    return {"release": release, "rendered": rendered}


def _ingress_rules(policy: dict) -> list[dict]:
    return policy["spec"]["ingress"]


def _endpoint_selectors(rules: list[dict]) -> list[dict]:
    out = []
    for rule in rules:
        for sel in rule.get("fromEndpoints", []):
            out.append(sel)
    return out


def _ports(rule: dict) -> set[str]:
    return {p["port"] for p in rule["toPorts"][0]["ports"]}


def test_policy_renders_at_all(prod):
    """Inertness guard: if the gate flips off, every other test passes vacuously."""
    policy = _api_ingress_policy(prod["rendered"], prod["release"])
    assert policy["spec"]["endpointSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "monolith",
        "app.kubernetes.io/instance": prod["release"],
        "app.kubernetes.io/component": "app",
    }


def test_no_new_namespace_wide_grants(prod):
    """A selector naming ONLY a namespace must stay an embervm-only shape.

    #4660 narrowed envoy-gateway-system and mcp off this list. If a third
    namespace shows up here, the diff deserves the same scrutiny: who in that
    namespace needs this pod, on which port, and does a direct grant bypass the
    gateway's X-Auth-Email strip?
    """
    ns_only = set()
    for sel in _endpoint_selectors(
        _ingress_rules(_api_ingress_policy(prod["rendered"], prod["release"]))
    ):
        labels = sel.get("matchLabels", {})
        namespaced = [k for k in labels if k.endswith(_NS_KEY)]
        if len(namespaced) == 1 and len(labels) == 1:
            ns_only.add(labels[namespaced[0]])
    assert ns_only == {"embervm"}, (
        f"namespace-wide ingress grants exist for {sorted(ns_only)}: each one "
        "lets every pod in that namespace send a forged X-Auth-Email. Narrow "
        "to the specific pods that need the port (issue #4660 pattern), or "
        "record the accepted cost next to the rule like the embervm entries "
        "do."
    )


def test_envoy_grant_is_proxy_pods_only(prod):
    """The envoy-gateway-system grant must name the proxy pod identity."""
    rules = _ingress_rules(_api_ingress_policy(prod["rendered"], prod["release"]))
    matches = [
        r
        for r in rules
        if any(
            s.get("matchLabels", {}).get(f"k8s:{_NS_KEY}") == "envoy-gateway-system"
            for s in r.get("fromEndpoints", [])
        )
    ]
    assert len(matches) == 1, "expected exactly one envoy-gateway-system rule"
    sel = matches[0]["fromEndpoints"][0]["matchLabels"]
    assert sel == {
        f"k8s:{_NS_KEY}": "envoy-gateway-system",
        # Stamped by Envoy Gateway itself, upstream v1.8.3,
        # internal/infrastructure/kubernetes/proxy/resource.go EnvoyAppLabel().
        # These keys CAN drift across an envoy-gateway upgrade. The #4659
        # tripwire that caught that on policy-denied drops was removed in #5362,
        # so this assertion is now the only guard
        # until replacement monitoring lands. The rollback is reverting these
        # two keys in the template.
        "app.kubernetes.io/name": "envoy",
        "app.kubernetes.io/component": "proxy",
    }
    assert _ports(matches[0]) == {"3000", "8000"}, (
        "the private HTTPRoute backendRefs both 3000 (frontend) and 8000 (api); "
        "dropping 8000 kills the API and the github webhook as a silent dial "
        "timeout"
    )


def test_cloudflared_is_never_granted(prod):
    """No rule may select the cloudflared tunnel pods.

    cloudflared dials only the envoy service on :80 (cloudflare-gateway
    values-prod catchAll). A direct grant would route around HTTPRoute routing
    AND the ClientTrafficPolicy that strips inbound X-Auth-Email at the envoy
    listener (#4660).
    """
    rendered = prod["rendered"]
    policy = _api_ingress_policy(rendered, prod["release"])
    for sel in _endpoint_selectors(_ingress_rules(policy)):
        labels = sel.get("matchLabels", {})
        assert labels.get("app.kubernetes.io/name") != "cloudflare-tunnel"
        assert labels.get("app") != "cloudflare-tunnel"


def test_mcp_grant_is_gateway_pod_only(prod):
    """The mcp grant must name Context Forge's gateway Deployment, not the ns."""
    chart_values = yaml.safe_load((_chart_dir() / "values.yaml").read_text())
    label = chart_values["ciliumPolicy"]["ingress"]["mcpGatewayPodLabel"]
    replay_label = chart_values["ciliumPolicy"]["tokenReplayDeny"]["gatewayPodLabel"]
    assert label == replay_label, (
        "tokenReplayDeny.gatewayPodLabel and ingress.mcpGatewayPodLabel name "
        "the same Context Forge gateway pod; they drifted apart, so one of the "
        "two policies now describes a pod that no longer exists"
    )

    rules = _ingress_rules(_api_ingress_policy(prod["rendered"], prod["release"]))
    matches = [
        r
        for r in rules
        if any(
            s.get("matchLabels", {}).get(f"k8s:{_NS_KEY}") == "mcp"
            for s in r.get("fromEndpoints", [])
        )
    ]
    assert len(matches) == 1, "expected exactly one mcp rule"
    sel = matches[0]["fromEndpoints"][0]["matchLabels"]
    assert sel == {f"k8s:{_NS_KEY}": "mcp", "app": label}
    assert _ports(matches[0]) == {"8000"}


def test_the_config_only_entries_survive(prod):
    """Entries invisible to short Hubble windows must not be tidied away.

    All three are known from config rather than observation: Argo workflows POST
    MONOLITH_INTERNAL_URL periodically, the whatsapp gateway posts inbound
    messages, and the embervm egress proxy forwards guest progress. Dropping
    any of them breaks that path the next time it runs, as a silent dial
    timeout.
    """
    release = prod["release"]
    chart_values = yaml.safe_load((_chart_dir() / "values.yaml").read_text())
    jobs_ns = chart_values["jobs"]["workflowNamespace"]

    rules = _ingress_rules(_api_ingress_policy(prod["rendered"], release))
    by_shape = {}
    for rule in rules:
        for sel in rule.get("fromEndpoints", []):
            by_shape.setdefault(
                frozenset(sel.get("matchLabels", {}).items()), set()
            ).update(_ports(rule))

    def find(labels: dict) -> set[str]:
        ports = by_shape.get(frozenset(labels.items()))
        assert ports is not None, (
            f"no allowlist entry matches {labels}; if you removed it on "
            "purpose, prove the caller is gone first (see the provenance "
            "header in cilium-ingress-policy.yaml)"
        )
        return ports

    assert find(
        {f"k8s:{_NS_KEY}": jobs_ns, "app.kubernetes.io/part-of": f"{release}-jobs"}
    ) == {"8000"}
    assert find(
        {
            "app.kubernetes.io/name": "monolith",
            "app.kubernetes.io/instance": release,
            "app.kubernetes.io/component": "whatsapp",
        }
    ) == {"8000"}
    # Two separate embervm entries share this selector shape: 8091 for guest
    # progress, 3000 for the shotter render. Together they must admit both
    # ports and still NOT 8000, the main API.
    assert find({f"k8s:{_NS_KEY}": "embervm"}) == {"3000", "8091"}
    assert not any(
        f"k8s:{_NS_KEY}" in sel.get("matchLabels", {})
        and len(sel.get("matchLabels", {})) == 1
        and sel["matchLabels"][f"k8s:{_NS_KEY}"] == "embervm"
        and "8000" in _ports(rule)
        for rule in rules
        for sel in rule.get("fromEndpoints", [])
    ), "the main API on 8000 must stay closed to embervm"


def test_host_and_health_entities_stay(prod):
    """kubelet probes originate on the node; without them the pod never goes Ready."""
    policy = _api_ingress_policy(prod["rendered"], prod["release"])
    entities = set()
    for rule in _ingress_rules(policy):
        entities.update(rule.get("fromEntities", []))
    assert {"host", "health"} <= entities


def test_dev_renders_the_same_invariant():
    """Dev layers prod's values, so it inherits the armed policy and the narrowing."""
    chart = _chart_dir()
    release = _release_name(
        Path(
            _env(
                "DEV_APPLICATION", chart.parent / "dev" / "deploy" / "application.yaml"
            )
        )
    )
    rendered = _render(
        release, [chart / "values.yaml", _deploy_values(), _dev_values()]
    )
    policy = _api_ingress_policy(rendered, release)
    for sel in _endpoint_selectors(_ingress_rules(policy)):
        labels = sel.get("matchLabels", {})
        namespaced = [k for k in labels if k.endswith(_NS_KEY)]
        if len(namespaced) == 1 and len(labels) == 1:
            assert labels[namespaced[0]] == "embervm", (
                f"dev renders a namespace-wide grant for "
                f"{labels[namespaced[0]]}; see test_no_new_namespace_wide_grants"
            )
