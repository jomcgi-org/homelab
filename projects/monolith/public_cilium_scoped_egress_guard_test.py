"""Guard (#5142, THREAT-MODEL finding 3): the public tier's destination-scoped
egress policies must stay destination-scoped.

``cilium-policy-scoped-egress.yaml`` exists to replace the broad
``toEntities: cluster`` grant with one ``toEndpoints`` rule per dependency. The
cheap way for that to regress is someone adding ``cluster`` (or ``world``) back
into the scoped file to make a dial timeout go away, which silently returns the
public tier to "anything in the cluster" reach. This reads the raw template
(Go-templated, so not parseable as YAML) and fails on that shape, and on a
scoped policy that forgot DNS, which is the first thing every pod needs.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

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
    raise FileNotFoundError(
        f"{TEMPLATE} not found at {here} or {candidate} (TEST_SRCDIR={srcdir!r})"
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
