"""Serve the friends Grimoire UI: static files built by ``web/grimoire``.

The UI is a client-only React app, so serving it is file delivery plus one
rule: any path under its base that is not a built asset answers with
``index.html``, and the client router takes it from there. The base path is
read from the built ``index.html`` itself, so the server can never disagree
with the bundle about where it lives.

Every response is private. The friends gateway route gates each byte with the
OIDC session, so a shared cache must never hold a copy.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

logger = logging.getLogger("monolith.grimoire.web")

_ASSET_BASE = re.compile(r'(?:src|href)="(/[A-Za-z0-9/_-]*/)assets/')
_ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

_STYLE_HASH = re.compile(r"^sha256-[A-Za-z0-9+/]{43}=$")


def content_security_policy(style_hashes: list[str]) -> str:
    """No inline script. Inline style only for the sheets the bundle lists.

    The component library injects one fixed sheet at runtime. The bundle ships
    its hash in ``style-hashes.json`` (kept current by ``src/csp.test.tsx``),
    so the policy names exactly that text and nothing else. Images also load
    from the API (handouts) and as blob: upload previews.
    """
    for value in style_hashes:
        if not _STYLE_HASH.match(value):
            raise ValueError(f"invalid style hash {value!r}")
    style = " ".join(["'self'", *(f"'{value}'" for value in style_hashes)])
    return "; ".join(
        [
            "default-src 'none'",
            "script-src 'self'",
            f"style-src {style}",
            "img-src 'self' blob: data:",
            "font-src 'self'",
            "connect-src 'self'",
            "media-src 'self' blob:",
            "form-action 'self'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        ]
    )


_PRIVATE = {
    "Cloudflare-CDN-Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}
_SHELL_HEADERS = {
    **_PRIVATE,
    # Revalidate on every load so a deploy takes effect on the next reload.
    "Cache-Control": "private, no-cache",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
_ASSET_HEADERS = {
    **_PRIVATE,
    # File names carry a content hash, so a browser may keep them for good.
    "Cache-Control": "private, max-age=31536000, immutable",
}


def dist_dir() -> Path:
    return Path(
        os.environ.get(
            "GRIMOIRE_WEB_DIR",
            str(Path(__file__).resolve().parents[1] / "web" / "grimoire" / "dist"),
        )
    )


def web_base(index_html: str) -> str:
    """The path prefix the bundle was built for, without a trailing slash."""
    match = _ASSET_BASE.search(index_html)
    if match is None:
        raise ValueError("index.html references no asset under a base path")
    return match.group(1).rstrip("/")


def build_router(directory: Path) -> APIRouter:
    index = (directory / "index.html").read_bytes()
    base = web_base(index.decode())
    assets = directory / "assets"
    hashes = directory / "style-hashes.json"
    shell_headers = {
        **_SHELL_HEADERS,
        "Content-Security-Policy": content_security_policy(
            json.loads(hashes.read_text()) if hashes.is_file() else []
        ),
    }
    router = APIRouter(include_in_schema=False)

    def shell(path: str = "") -> Response:
        # Nothing under assets/ is an app route, however the path is spelled.
        if path == "assets" or path.startswith("assets/"):
            raise HTTPException(404, "asset not found", headers=_PRIVATE)
        return Response(index, media_type="text/html", headers=shell_headers)

    @router.get(f"{base}/assets/{{name}}")
    def asset(name: str) -> FileResponse:
        # A missing asset is a 404, never the HTML shell: a script tag that
        # receives HTML fails in a way that hides the real cause.
        path = assets / name
        if not _ASSET_NAME.match(name) or not path.is_file():
            raise HTTPException(404, "asset not found", headers=_PRIVATE)
        return FileResponse(path, headers=_ASSET_HEADERS)

    router.add_api_route(base, shell, methods=["GET"])
    router.add_api_route(f"{base}/{{path:path}}", shell, methods=["GET"])
    logger.info("Serving the Grimoire web app at %s from %s", base, directory)
    return router


def mount_web(app: FastAPI) -> None:
    """Register the UI when its bundle is present (it is absent in most tests)."""
    directory = dist_dir()
    if (directory / "index.html").is_file():
        app.include_router(build_router(directory))
    elif os.environ.get("GRIMOIRE_BROWSER_ORIGIN"):
        # The gateway routes the UI here, so a missing bundle is an outage.
        logger.error("Grimoire web bundle not found at %s", directory)
