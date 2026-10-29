"""Artifact HTTP surface (ADR 024 decisions 3 + 4).

The read router is mounted on both binaries under ``/internal/artifact``.
The gateway never routes ``/internal``; the SSR frontend proxies ``/raw`` and
``/version`` and is the sole public origin. ``/raw`` serves stored HTML with a
strict CSP; ``/version`` returns the ETag the hot-reload poller watches.

Agent HTML is untrusted (ADR 024 decision 4): ``/raw`` carries a ``sandbox``
CSP + locked-down ``default-src`` so even a directly-fetched artifact runs with
no ambient authority, and the SSR wrapper frames it with ``sandbox=allow-scripts``
(no ``allow-same-origin``) for an opaque origin.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Response

# Artifact ids are capability URLs (unguessable id == access). This charset
# keeps an id safe as both an S3 key segment and a URL path segment (no traversal,
# no separators).
_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

# The CSP on the raw artifact response. The PRIMARY isolation is the sandboxed
# iframe in +page.svelte: `sandbox allow-scripts` (no `allow-same-origin`) gives
# the document an opaque origin, so it can never read jomcgi.dev cookies/storage/
# DOM. That boundary is non-negotiable (ADR 024 decision 4) and is what protects
# our origin. The CSP here is deliberately permissive about the open web so an
# artifact can behave like a normal page: load libraries from CDNs, pull web
# fonts, fetch live data, and refresh from APIs (https only). `default-src 'none'`
# stays as the backstop and each capability is opened explicitly; `http:` is
# withheld to block plaintext/LAN-probe downgrades; `form-action`/`base-uri` stay
# locked to keep top-window phishing-form posts out. Opening `connect-src`
# (re)accepts the viewer-side beacon/exfil risk ADR 024 flagged. This risk is
# re-accepted even though artifact creation is no longer owner-only (it is now a
# per-server ACL feature, ADR 029, so a trusted collaborator on a granted server
# can author artifacts): the residual exposure is only that a viewer who opens a
# shared artifact link could have their in-page activity beaconed out by that
# artifact's author, the same trust you extend by opening any link a person
# sends you. It is bounded because (a) authors are ACL-granted, semi-trusted
# people, (b) artifact URLs are unguessable capability tokens shared by link, not
# publicly discoverable, and (c) the opaque sandbox origin still denies ANY
# access to our origin's cookies/storage/DOM (ADR 024 decision 4, non-negotiable,
# unchanged). See the ADR 024 amendments (2026-06-29 CSP; 2026-07-02 capability
# URLs + per-server /artifact).
_ARTIFACT_CSP = (
    "sandbox allow-scripts; default-src 'none'; "
    "script-src 'unsafe-inline' https:; style-src 'unsafe-inline' https:; "
    "img-src data: blob: https:; font-src data: https:; "
    "connect-src https:; form-action 'none'; base-uri 'none'"
)


read_router = APIRouter(prefix="/internal/artifact", tags=["artifact"])


def _require_valid_id(artifact_id: str) -> str:
    if not _ID_RE.match(artifact_id):
        raise HTTPException(status_code=404, detail="not found")
    return artifact_id


@read_router.get("/{artifact_id}/raw")
def get_artifact_raw(artifact_id: str) -> Response:
    """Serve the raw artifact HTML with a strict CSP (untrusted; ADR 024)."""
    from artifact import s3

    _require_valid_id(artifact_id)
    got = s3.get_artifact(artifact_id)
    if got is None:
        raise HTTPException(status_code=404, detail="not found")
    html, _etag = got
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": _ARTIFACT_CSP,
            # The wrapper hot-reloads via the version endpoint, so the framed doc
            # must never be cached stale.
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@read_router.get("/{artifact_id}/version")
def get_artifact_version(artifact_id: str) -> Response:
    """Return the artifact's current version (ETag) for the hot-reload poller."""
    from artifact import s3

    _require_valid_id(artifact_id)
    version = s3.head_artifact(artifact_id)
    if version is None:
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        content=json.dumps({"version": version}),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )
