"""Focused render and semantics guards for the public FaaS edge budget.

The shipping chart must give each valid Cloudflare client address its own
120-per-minute local descriptor while retaining a separate fail-safe bucket for
missing or malformed identity. The page, ember-read, private, and task-class
routes are deliberately outside that selector's target.

The behavioral test below exercises the consequences of the rendered rules; it
does not pretend to run an Envoy data plane. Runtime support is grounded
separately by checking the exact Envoy Gateway 1.8.3 CRD bundle installed by the
repository. Envoy Gateway's own pinned distinct-header end-to-end test is the
runtime evidence for descriptor isolation.
"""

from __future__ import annotations

import os
import re
import subprocess
import tarfile
from collections import defaultdict
from pathlib import Path

import yaml

_CRD_TARBALL = "gateway-crds-helm-1.8.3.tgz"


def _runfile(relative: str) -> Path:
    srcdir = os.environ.get("TEST_SRCDIR", "")
    candidate = Path(srcdir) / "_main" / relative
    if candidate.exists():
        return candidate
    root = Path(__file__).resolve().parents[2]
    local = root / relative
    if local.exists():
        return local
    raise FileNotFoundError(f"could not find {relative} at {candidate} or {local}")


def _chart_dir() -> Path:
    return _runfile("projects/monolith-public/chart/Chart.yaml").parent


def _deploy_values() -> Path:
    configured = os.environ.get("DEPLOY_VALUES")
    if configured:
        path = Path(configured)
        if path.exists():
            return path
    return _runfile("projects/monolith-public/deploy/values.yaml")


def _crd_tarball() -> Path:
    return _runfile(f"projects/platform/cloudflare-gateway/charts/{_CRD_TARBALL}")


def _render() -> list[dict]:
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
            str(_deploy_values()),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [doc for doc in docs if doc.get("kind") == kind]


def _policies_by_target(docs: list[dict]) -> dict[str, dict]:
    policies = _by_kind(docs, "BackendTrafficPolicy")
    return {policy["spec"]["targetRefs"][0]["name"]: policy for policy in policies}


def _functions_rules(docs: list[dict]) -> list[dict]:
    policy = _policies_by_target(docs)["monolith-public-functions"]
    return policy["spec"]["rateLimit"]["local"]["rules"]


def test_pinned_envoy_gateway_schema_supports_rendered_selectors():
    """Reject fields invented beyond the exact installed 1.8.3 CRD schema."""
    with tarfile.open(_crd_tarball(), "r:gz") as archive:
        member = next(
            item
            for item in archive.getmembers()
            if item.name.endswith("backendtrafficpolicies.yaml")
        )
        handle = archive.extractfile(member)
        assert handle is not None
        raw = handle.read().decode()
        crd = yaml.safe_load(
            "\n".join(line for line in raw.splitlines() if not line.startswith("{{-"))
        )

    version = next(
        item for item in crd["spec"]["versions"] if item["name"] == "v1alpha1"
    )
    properties = version["schema"]["openAPIV3Schema"]["properties"]
    header_match = properties["spec"]["properties"]["rateLimit"]["properties"]["local"][
        "properties"
    ]["rules"]["items"]["properties"]["clientSelectors"]["items"]["properties"][
        "headers"
    ]["items"]["properties"]
    assert {"Distinct", "RegularExpression"} <= set(header_match["type"]["enum"])
    assert header_match["value"]["maxLength"] == 1024


def test_rendered_functions_budget_uses_validated_cloudflare_identity():
    rules = _functions_rules(_render())
    assert len(rules) == 2

    fallback, per_client = rules
    assert "clientSelectors" not in fallback
    assert fallback["limit"] == {"requests": 120, "unit": "Minute"}
    assert per_client["limit"] == fallback["limit"]

    headers = per_client["clientSelectors"][0]["headers"]
    assert headers[0] == {"name": "CF-Connecting-IP", "type": "Distinct"}
    assert headers[1]["name"] == "CF-Connecting-IP"
    assert headers[1]["type"] == "RegularExpression"
    pattern = headers[1]["value"]
    assert len(pattern) <= 1024
    for address in ("203.0.113.7", "2001:db8::7", "::1"):
        assert re.fullmatch(pattern, address), address
    for malformed in ("", "unknown", "256.1.1.1", "1.2.3.4:80", "2001:db8:::7"):
        assert not re.fullmatch(pattern, malformed), malformed


def test_client_selector_is_scoped_only_to_the_public_functions_route():
    docs = _render()
    policies = _policies_by_target(docs)
    assert set(policies) == {
        "monolith-public-public",
        "monolith-public-functions",
        "monolith-public-ember-reads",
    }

    selected = [
        target
        for target, policy in policies.items()
        if any(
            "clientSelectors" in rule
            for rule in policy["spec"]["rateLimit"]["local"]["rules"]
        )
    ]
    assert selected == ["monolith-public-functions"]

    routes = {doc["metadata"]["name"]: doc for doc in _by_kind(docs, "HTTPRoute")}
    function_route = routes["monolith-public-functions"]
    paths = [
        match["path"]["value"]
        for rule in function_route["spec"]["rules"]
        for match in rule["matches"]
    ]
    assert paths == ["/functions/"]

    # Shared library consumers keep their existing single default buckets.
    assert policies["monolith-public-public"]["spec"]["rateLimit"]["local"][
        "rules"
    ] == [{"limit": {"requests": 100, "unit": "Minute"}}]
    assert policies["monolith-public-ember-reads"]["spec"]["rateLimit"]["local"][
        "rules"
    ] == [{"limit": {"requests": 600, "unit": "Minute"}}]


def test_rendered_budget_model_keeps_clients_and_fallback_independent():
    """Exercise independent counters from the actual rendered rule inputs.

    Envoy Gateway 1.8.3 translates descriptor matches with
    ``always_consume_default_token_bucket=false``. Therefore valid addresses
    use the Distinct header value as their counter key, while unmatched values
    use only the default fallback counter.
    """
    fallback, per_client = _functions_rules(_render())
    limit = per_client["limit"]["requests"]
    assert fallback["limit"]["requests"] == limit == 120
    pattern = per_client["clientSelectors"][0]["headers"][1]["value"]
    counters: defaultdict[tuple[str, str], int] = defaultdict(int)

    def allowed(identity: str | None) -> bool:
        key = (
            ("client", identity)
            if identity is not None and re.fullmatch(pattern, identity)
            else ("fallback", "invalid-or-missing")
        )
        if counters[key] >= limit:
            return False
        counters[key] += 1
        return True

    abusive = "203.0.113.10"
    unrelated = "198.51.100.20"
    assert all(allowed(abusive) for _ in range(limit))
    assert not allowed(abusive)
    assert allowed(unrelated)

    assert all(allowed(None) for _ in range(limit))
    assert not allowed(None)
    assert not allowed("malformed")
    assert allowed("2001:db8::20")
    assert allowed(unrelated)
