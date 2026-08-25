"""Guard (#5142, security finding #5276): the public tier's destination-scoped
egress policies must stay destination-scoped.

``cilium-policy-scoped-egress.yaml`` exists to replace the broad
``toEntities: cluster`` grant with one ``toEndpoints`` rule per dependency. The
cheap way for that to regress is someone adding ``cluster`` (or ``world``) back
into the scoped file to make a dial timeout go away, which silently returns the
public tier to "anything in the cluster" reach. This reads the raw template
(Go-templated, so not parseable as YAML) and fails on that shape, and on a
scoped policy that forgot DNS, which is the first thing every pod needs.

The second half runs `helm template` against chart/values.yaml PLUS the live
deploy/values.yaml and pins the rendered policy shape in all three rollout
modes. Enforce mode (the shipped shape) is pinned hardest: no ``toEntities``
beyond the hostNetwork OTLP agent's node entities anywhere, the broad policies
gone, and the exact per-dependency allow set the audit window cleared. A
dependency dropped from scopedTargets now fails HERE instead of as a
production silent dial timeout.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

TEMPLATE = "cilium-policy-scoped-egress.yaml"


def _template_path() -> Path:
    here = Path(__file__).resolve().parent / "templates" / TEMPLATE
    if here.exists():
        return here
    srcdir = os.environ.get("TEST_SRCDIR", "")
    candidate = (
        Path(srcdir)
        / "_main"
        / "projects"
        / "monolith-public"
        / "chart"
        / "templates"
        / TEMPLATE
    )
    if candidate.exists():
        return candidate
    # Plain pytest against the worktree: the chart lives one directory over.
    worktree = (
        Path(__file__).resolve().parent.parent
        / "monolith-public"
        / "chart"
        / "templates"
        / TEMPLATE
    )
    if worktree.exists():
        return worktree
    raise FileNotFoundError(
        f"{TEMPLATE} not found at {here}, {candidate} or {worktree} "
        f"(TEST_SRCDIR={srcdir!r})"
    )


def _policies(text: str) -> list[str]:
    """Split the template into one chunk per CiliumNetworkPolicy document."""
    chunks = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    return [c for c in chunks if "kind: CiliumNetworkPolicy" in c]


def _entities(chunk: str) -> set[str]:
    found: set[str] = set()
    for block in re.finditer(r"toEntities:\n((?:\s+- \S+\n)+)", chunk):
        found.update(re.findall(r"- (\S+)", block.group(1)))
    return found


# ---------------------------------------------------------------------------
# Render-level pins (helm template against the real values).
# ---------------------------------------------------------------------------


def _chart_dir() -> Path:
    here = Path(__file__).resolve().parent
    local = here.parent / "monolith-public" / "chart"
    if (local / "Chart.yaml").exists():
        return local
    srcdir = os.environ.get("TEST_SRCDIR", "")
    candidate = Path(srcdir) / "_main" / "projects" / "monolith-public" / "chart"
    if (candidate / "Chart.yaml").exists():
        return candidate
    raise FileNotFoundError(f"chart not found at {local} or {candidate}")


def _deploy_values_path() -> Path:
    env = os.environ.get("DEPLOY_VALUES")
    if env:
        return Path(env)
    local = (
        Path(__file__).resolve().parent.parent
        / "monolith-public"
        / "deploy"
        / "values.yaml"
    )
    if local.exists():
        return local
    raise FileNotFoundError("deploy values not found and DEPLOY_VALUES unset")


def _render(extra_values: dict | None = None) -> list[dict]:
    """helm template with chart values + deploy values (+ an optional override).

    Returns every non-null YAML document of the render.
    """
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "monolith-public",
        str(_chart_dir()),
        "--namespace",
        "monolith-public",
        "--values",
        str(_chart_dir() / "values.yaml"),
        "--values",
        str(_deploy_values_path()),
    ]
    override_path = None
    if extra_values:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            yaml.safe_dump(extra_values, handle)
            override_path = handle.name
        argv += ["--values", override_path]
    try:
        result = subprocess.run(argv, capture_output=True, text=True)
    finally:
        if override_path:
            os.unlink(override_path)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [d for d in yaml.safe_load_all(result.stdout) if d is not None]


def _cnps(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("kind") == "CiliumNetworkPolicy"]


def _by_name(cnps: list[dict], suffix: str) -> dict:
    matches = [d for d in cnps if d["metadata"]["name"].endswith(suffix)]
    assert len(matches) == 1, (
        f"expected exactly one CiliumNetworkPolicy named *{suffix}, got "
        f"{[d['metadata']['name'] for d in matches]}"
    )
    return matches[0]


def _fmt(rules: set[tuple]) -> list[str]:
    """Readable sort for rule sets; the frontend's same-namespace rule has a
    None namespace, which sorted() refuses to order against strings."""
    return sorted((repr(r) for r in rules))


# DNS allow, in the order _endpoint_rules normalises to.
DNS_PORTS = (("53", "TCP"), ("53", "UDP"))


def _endpoint_rules(doc: dict) -> set[tuple]:
    """Every (namespace, selector labels, ports) triple a policy allows."""
    rules: set[tuple] = set()
    for rule in doc.get("spec", {}).get("egress", []) or []:
        for endpoint in rule.get("toEndpoints", []) or []:
            labels = dict(endpoint.get("matchLabels", {}))
            namespace = labels.pop("k8s:io.kubernetes.pod.namespace", None)
            ports = tuple(
                sorted(
                    (str(port["port"]), port["protocol"])
                    for block in rule.get("toPorts", []) or []
                    for port in block.get("ports", [])
                )
            )
            rules.add((namespace, tuple(sorted(labels.items())), ports))
    return rules


def _entities_in(doc: dict) -> set[str]:
    found: set[str] = set()
    for rule in doc.get("spec", {}).get("egress", []) or []:
        found.update(rule.get("toEntities", []) or [])
    return found


def _declared_targets() -> dict:
    values = yaml.safe_load((_chart_dir() / "values.yaml").read_text())
    return values["ciliumPolicy"]["egress"]["scopedTargets"]


def _expected_rule(target: dict, ports: tuple) -> tuple:
    return (
        target["namespace"],
        tuple(sorted(target["matchLabels"].items())),
        ports,
    )


def test_scoped_egress_never_grants_cluster_or_world():
    text = _template_path().read_text()
    policies = _policies(text)
    assert len(policies) == 3, "expected web, frontend and imgproxy scoped policies"
    for chunk in policies:
        entities = _entities(chunk)
        assert "cluster" not in entities, (
            "scoped egress must not grant toEntities: cluster"
        )
        assert "world" not in entities, "scoped egress must not grant toEntities: world"
        assert "all" not in entities, "scoped egress must not grant toEntities: all"


def test_host_entities_are_port_pinned():
    """`host`/`remote-node` survive only for the hostNetwork OTLP agent, on one port."""
    text = _template_path().read_text()
    for match in re.finditer(r"toEntities:\n((?:\s+- \S+\n)+)(\s+toPorts:)?", text):
        assert match.group(2), "a toEntities rule in the scoped file must carry toPorts"


def test_every_scoped_policy_allows_dns():
    text = _template_path().read_text()
    for chunk in _policies(text):
        assert '- port: "53"' in chunk, (
            "a scoped egress policy without DNS fails every lookup"
        )
        assert "$dns.matchLabels" in chunk


def test_audit_mode_is_additive():
    """Audit mode must not flip an endpoint to default-deny on its own."""
    text = _template_path().read_text()
    for chunk in _policies(text):
        assert "enableDefaultDeny:" in chunk
        assert re.search(r"enableDefaultDeny:\n\s+egress: false", chunk)


def _enforced_render() -> list[dict]:
    """The shipped shape: deploy/values.yaml says scoped: enforce."""
    docs = _render()
    scoped = [
        d["metadata"]["name"]
        for d in _cnps(docs)
        if d["metadata"]["name"].endswith("-egress-scoped")
    ]
    assert len(scoped) == 3, (
        f"enforce mode must render the three scoped egress policies, got {scoped}; "
        "is deploy/values.yaml still on scoped: enforce?"
    )
    return docs


def test_enforced_render_has_no_cluster_grant_on_any_pod():
    """The #5142 acceptance criterion, on the real deploy values."""
    for doc in _cnps(_enforced_render()):
        entities = _entities_in(doc)
        assert "cluster" not in entities, (
            f"{doc['metadata']['name']} grants toEntities: cluster; the public "
            "tier must stay destination-scoped"
        )
        assert not ({"world", "all"} & entities), (
            f"{doc['metadata']['name']} grants {sorted(entities)}"
        )
    # Guard the guard: enforce mode must actually select endpoints, otherwise
    # the loop above passes while nothing is enforced.
    assert _cnps(_enforced_render()), "no CiliumNetworkPolicy rendered at all"


def test_enforced_render_drops_the_broad_egress_policies():
    names = {d["metadata"]["name"] for d in _cnps(_enforced_render())}
    for broad in ("-web-egress", "-frontend-egress", "-imgproxy-egress"):
        assert not any(n.endswith(broad) for n in names), (
            f"{broad} still renders under scoped: enforce; the broad "
            "toEntities rules must yield entirely to the scoped policies"
        )


def test_enforced_web_policy_pins_exactly_the_audited_destinations():
    """The load-bearing pin: web's allows equal the audited inventory.

    Derived from scopedTargets itself (structural half), then compared against
    a hardcoded expected set (semantic half). The first catches template
    drift; the second catches an accidental addition or removal in values,
    either of which ships as a silent dial timeout or as dead surface.
    """
    doc = _by_name(_cnps(_enforced_render()), "-web-egress-scoped")
    targets = _declared_targets()
    expected_from_values = {
        _expected_rule(targets["dns"], DNS_PORTS),
        *(
            _expected_rule(targets[key], ((str(targets[key]["port"]), "TCP"),))
            for key in (
                "postgres",
                "inference",
                "embeddings",
                "seaweedfs",
                "embervm",
                "embervmServing",
            )
        ),
    }
    rendered = _endpoint_rules(doc)
    assert rendered == expected_from_values, (
        "web scoped egress drifted from scopedTargets:\n"
        f"  only in render: {_fmt(rendered - expected_from_values)}\n"
        f"  only in values: {_fmt(expected_from_values - rendered)}"
    )

    expected_semantic = {
        # kube-dns, both protocols.
        ("kube-system", (("k8s-app", "kube-dns"),), DNS_PORTS),
        # CNPG instance pods directly (no pooler): ro + rw credentials.
        ("monolith", (("cnpg.io/cluster", "monolith-pg"),), (("5432", "TCP"),)),
        # vLLM inference and llama.cpp embeddings.
        (
            "inference",
            (
                ("app.kubernetes.io/component", "inference"),
                ("app.kubernetes.io/name", "inference"),
            ),
            (("8080", "TCP"),),
        ),
        (
            "inference",
            (
                ("app.kubernetes.io/component", "embeddings"),
                ("app.kubernetes.io/name", "inference"),
            ),
            (("8080", "TCP"),),
        ),
        # SeaweedFS S3 gateway.
        (
            "seaweedfs",
            (
                ("app.kubernetes.io/component", "s3"),
                ("app.kubernetes.io/name", "seaweedfs"),
            ),
            (("8333", "TCP"),),
        ),
        # EmberVM control plane (/functions router, demo-postgres status/reset).
        (
            "embervm",
            (
                ("app.kubernetes.io/instance", "embervm"),
                ("app.kubernetes.io/name", "embervm"),
            ),
            (("8080", "TCP"),),
        ),
        # EmberVM SERVING Envoy DaemonSet (demo-postgres DSN, distinct selector
        # AND port from the control plane).
        (
            "embervm",
            (
                ("app.kubernetes.io/instance", "embervm"),
                ("app.kubernetes.io/name", "embervm-serving-envoy"),
            ),
            (("5401", "TCP"),),
        ),
    }
    assert rendered == expected_semantic, (
        "web scoped egress drifted from the audited destination inventory:\n"
        f"  unexpected: {_fmt(rendered - expected_semantic)}\n"
        f"  missing: {_fmt(expected_semantic - rendered)}\n"
        "If you added or removed a dependency deliberately, re-run the Hubble "
        "audit before changing this set."
    )

    # Turnstile siteverify moves here in enforce mode: Cloudflare CIDRs on 443.
    cidr_rules = [rule for rule in doc["spec"]["egress"] if rule.get("toCIDRSet")]
    assert len(cidr_rules) == 1, "exactly one toCIDRSet rule (Turnstile) expected"
    ports = {
        (str(p["port"]), p["protocol"])
        for block in cidr_rules[0]["toPorts"]
        for p in block["ports"]
    }
    assert ports == {("443", "TCP")}, f"Turnstile allow must be 443/TCP, got {ports}"
    assert any("173.245.48.0/20" in c["cidr"] for c in cidr_rules[0]["toCIDRSet"]), (
        "the Cloudflare published-range list lost its first entry"
    )

    # Enforce mode carries the deny: no additive escape hatch left behind.
    assert "enableDefaultDeny" not in doc["spec"], (
        "enforce mode must not render enableDefaultDeny.egress: false"
    )


def test_enforced_frontend_and_imgproxy_policies_pin_their_shapes():
    cnps = _cnps(_enforced_render())
    targets = _declared_targets()

    frontend = _by_name(cnps, "-frontend-egress-scoped")
    frontend_expected = {
        _expected_rule(targets["dns"], DNS_PORTS),
        # Same-namespace backend Service: no namespace label on this selector.
        (
            None,
            (
                ("app.kubernetes.io/component", "web"),
                ("app.kubernetes.io/instance", "monolith-public"),
                ("app.kubernetes.io/name", "monolith-public"),
            ),
            (("8000", "TCP"),),
        ),
    }
    assert _endpoint_rules(frontend) == frontend_expected, (
        f"frontend scoped egress drifted: {_fmt(_endpoint_rules(frontend))}"
    )
    # The ONLY entity grant in the whole tier: the hostNetwork OTLP agent,
    # reachable as the node itself, pinned to its single HTTP port.
    assert _entities_in(frontend) == {"host", "remote-node"}, (
        f"frontend entity grants drifted: {sorted(_entities_in(frontend))}"
    )
    entity_ports = {
        (str(p["port"]), p["protocol"])
        for rule in frontend["spec"]["egress"]
        if rule.get("toEntities")
        for block in rule.get("toPorts", [])
        for p in block["ports"]
    }
    assert entity_ports == {(str(targets["otelAgentHostPort"]), "TCP")}, (
        f"host/remote-node must stay pinned to the OTLP agent port, got {entity_ports}"
    )

    imgproxy = _by_name(cnps, "-imgproxy-egress-scoped")
    imgproxy_expected = {
        _expected_rule(targets["dns"], DNS_PORTS),
        _expected_rule(
            targets["seaweedfs"], ((str(targets["seaweedfs"]["port"]), "TCP"),)
        ),
    }
    assert _endpoint_rules(imgproxy) == imgproxy_expected, (
        f"imgproxy scoped egress drifted: {_fmt(_endpoint_rules(imgproxy))}"
    )
    assert not (_entities_in(imgproxy)), "imgproxy needs no entity grants"


def test_audit_mode_still_coexists_with_the_broad_policies():
    """The intermediate rung keeps its additive shape and today's enforcement."""
    docs = _render({"ciliumPolicy": {"egress": {"enabled": True, "scoped": "audit"}}})
    cnps = _cnps(docs)
    for suffix in ("-web-egress", "-frontend-egress", "-imgproxy-egress"):
        broad = _by_name(cnps, suffix)
        assert "cluster" in _entities_in(broad), (
            f"{broad['metadata']['name']} lost the wide cluster grant; audit "
            "mode changes correlation only, never enforcement"
        )
    for suffix in (
        "-web-egress-scoped",
        "-frontend-egress-scoped",
        "-imgproxy-egress-scoped",
    ):
        scoped = _by_name(cnps, suffix)
        deny = scoped["spec"].get("enableDefaultDeny")
        assert deny == {"egress": False}, (
            f"{scoped['metadata']['name']} must stay additive in audit mode, got {deny}"
        )


def test_off_mode_renders_the_pre_scoping_shape():
    docs = _render({"ciliumPolicy": {"egress": {"enabled": True, "scoped": "off"}}})
    names = {d["metadata"]["name"] for d in _cnps(docs)}
    assert not any(n.endswith("-egress-scoped") for n in names), (
        "scoped: off must render no scoped policies"
    )
    for suffix in ("-web-egress", "-frontend-egress", "-imgproxy-egress"):
        _by_name([d for d in _cnps(docs)], suffix)


def test_invalid_mode_fails_the_render():
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith-public",
            str(_chart_dir()),
            "--namespace",
            "monolith-public",
            "--values",
            str(_chart_dir() / "values.yaml"),
            "--values",
            str(_deploy_values_path()),
            "--set",
            "ciliumPolicy.egress.enabled=true",
            "--set",
            "ciliumPolicy.egress.scoped=yolo",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "an unknown scoped mode must fail the render"
    assert "off, audit or enforce" in result.stderr
