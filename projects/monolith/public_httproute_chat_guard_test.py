"""Phase 1 guard (ADR 005): the public HTTPRoute must never expose chat.

The only internet-facing origin for public chat is the SvelteKit SSR app; the
internal ``/internal/chat/*`` API is reachable solely in-cluster (Cilium
datapath) and must never appear on the public HTTPRoute. This test reads the chart
template and fails CI if a future edit adds a route path that references chat or
the internal API prefix, so the front-door invariant cannot regress silently.

The template is Go-templated (not valid YAML on its own), so the assertions are
over the raw ``path: value:`` entries rather than a parsed manifest.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def _template_path() -> Path:
    here = Path(__file__).resolve().parent / "templates" / "httproute-public.yaml"
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
        / "httproute-public.yaml"
    )
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f"httproute-public.yaml not found at {here} or {candidate} "
        f"(TEST_SRCDIR={srcdir!r})"
    )


def test_public_httproute_does_not_route_chat():
    text = _template_path().read_text()

    # Every HTTPRoute match path value (e.g. "/_app/", "/").
    path_values = re.findall(r"value:\s*(\S+)", text)
    assert path_values, "expected at least one path value in the HTTPRoute"
    for value in path_values:
        lowered = value.lower()
        assert "chat" not in lowered, f"chat exposed via path {value!r}"
        assert "internal" not in lowered, f"internal API exposed via path {value!r}"

    # Belt-and-braces: the internal chat prefix must not appear anywhere in the
    # template, even outside a path block.
    assert "/internal/chat" not in text
    assert "/internal" not in text
