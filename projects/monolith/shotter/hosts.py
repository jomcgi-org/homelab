"""Host-to-internal-service mapping for the website snapshotter (ADR embervm/035).

Public hostnames are mapped to internal Kubernetes services rather than
dialled through Cloudflare (ADR embervm/035 section 3): ``private.jomcgi.dev``
sits behind Cloudflare Access, so a credential-less browser going out through
the CDN would faithfully screenshot the login wall and report it as a
successful capture.

This is monolith's own copy of the mapping, the SECOND of two layers (ADR
embervm/035 section 4). It validates a requested URL before EmberVM is ever
dialled. The PRIMARY control is the in-guest proxy's hard allowlist, Go code
running inside the Firecracker guest (issue #4994 T2), covered by its own Go
test in that package. A Python spec cannot observe the guest's behaviour, and
asserting on a Python stand-in would prove the shape of the seam while
proving nothing about what the guest actually enforces.
"""

from __future__ import annotations

# Exactly two entries. A third is a deliberate security decision (it widens
# what a rendered page's top-level request is allowed to be), not a routine
# addition, and should be recorded (ADR amendment) before it lands, which is
# also why the domain's tests assert this map's exact size and exact values
# rather than merely "contains at least".
HOST_SERVICE_MAP: dict[str, str] = {
    "jomcgi.dev": "monolith-public-frontend.monolith-public.svc.cluster.local:3000",
    "private.jomcgi.dev": "monolith.monolith.svc.cluster.local:3000",
}
