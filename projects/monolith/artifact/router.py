"""Artifact HTTP surface (ADR 024 decisions 3 + 4).

Two routers, both under the ``/internal/artifact`` prefix so neither is on the
public HTTPRoute (in-cluster only, like chat_public): the gateway never routes
``/internal`` and the SSR frontend is the sole public origin.

- ``write_router`` (``POST /internal/artifact``): the agent's publish path. The
  monolith performs the S3 write, so the guest holds no S3 credential. Mounted on
  the FULL monolith only (the public tier stays read-only, ADR 004).
- ``read_router`` (``GET /internal/artifact/{id}/raw`` + ``/version``): the read
  path the public SSR frontend proxies. ``/raw`` serves the agent HTML with a
  strict CSP; ``/version`` is the ETag the hot-reload poller watches. Mounted on
  both binaries.

Agent HTML is untrusted (ADR 024 decision 4): ``/raw`` carries a ``sandbox``
CSP + locked-down ``default-src`` so even a directly-fetched artifact runs with
no ambient authority, and the SSR wrapper frames it with ``sandbox=allow-scripts``
(no ``allow-same-origin``) for an opaque origin.
"""

from __future__ import annotations

import json
import os
import re
import secrets

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

# Artifact ids are capability URLs (unguessable id == access), so generated ids
# are random url-safe tokens. A caller-supplied id must match this charset to
# keep it safe as both an S3 key segment and a URL path segment (no traversal,
# no separators).
_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

# Max stored artifact size. Self-contained HTML pages are small; this bounds a
# runaway/abusive publish and the S3 object size.
_MAX_HTML_BYTES = 2 * 1024 * 1024

# The strict CSP on the raw artifact response. `sandbox allow-scripts` gives the
# document an opaque origin with scripting but no same-origin/forms/popups/
# top-navigation; `default-src 'none'` denies everything not explicitly allowed;
# `connect-src 'none'` blocks beaconing/exfiltration (ADR 024 risk table). Inline
# script/style are allowed so a self-contained artifact runs; data:/blob: images
# cover inline assets.
_ARTIFACT_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'"
)


def _public_base() -> str:
    return os.environ.get("ARTIFACT_PUBLIC_BASE", "https://jomcgi.dev").rstrip("/")


class PublishRequest(BaseModel):
    html: str = Field(..., description="The self-contained artifact HTML document.")
    id: str | None = Field(
        default=None,
        description="Optional artifact id to (re)publish; server-assigned when absent.",
    )


class PublishResponse(BaseModel):
    id: str
    url: str
    version: str


write_router = APIRouter(prefix="/internal/artifact", tags=["artifact"])
read_router = APIRouter(prefix="/internal/artifact", tags=["artifact"])


@write_router.post("", response_model=PublishResponse)
def publish_artifact(req: PublishRequest) -> PublishResponse:
    """Publish (or re-publish) an artifact to object storage, return its URL."""
    from artifact import s3

    html_bytes = req.html.encode("utf-8")
    if not html_bytes:
        raise HTTPException(status_code=422, detail="empty artifact html")
    if len(html_bytes) > _MAX_HTML_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"artifact exceeds {_MAX_HTML_BYTES} bytes",
        )

    if req.id is None:
        artifact_id = secrets.token_urlsafe(9)
    else:
        if not _ID_RE.match(req.id):
            raise HTTPException(status_code=422, detail="invalid artifact id")
        artifact_id = req.id

    version = s3.put_artifact(artifact_id, html_bytes)
    return PublishResponse(
        id=artifact_id,
        url=f"{_public_base()}/artifact/{artifact_id}",
        version=version,
    )


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
