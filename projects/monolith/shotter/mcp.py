"""MCP tool that captures a screenshot of a deployed jomcgi.dev page.

ADR embervm/035: an EmberVM task-class guest runs headless Chromium and
renders one URL to a PNG on request. This module validates the requested URL
(the second, independent layer of ADR embervm/035 section 4, not the primary
control) and shapes the guest's response into a real FastMCP ImageContent
block plus a stored SeaweedFS URL (ADR embervm/035 section 5).
"""

from __future__ import annotations

import base64
import logging
from urllib.parse import urlsplit

from mcp.types import ImageContent

from core.mcp_app import mcp
from shotter import client, s3
from shotter.hosts import HOST_SERVICE_MAP

logger = logging.getLogger(__name__)

# Generous enough for a 4K capture (3840x2160), tight enough to reject an
# abusive or accidental request outright rather than silently clamping it. A
# silent clamp would hide the caller's mistake instead of surfacing it.
_MAX_DIMENSION = 4096

_DEFAULT_WIDTH = 1024
_DEFAULT_HEIGHT = 768
_DEFAULT_TIMEOUT_MS = 30_000


class InvalidShotterURL(ValueError):
    """The requested URL fails shotter's scheme, host, or shape validation."""


class InvalidViewportDimension(ValueError):
    """The requested viewport width or height is out of bounds."""


def validate_screenshot_url(url: str, width: int, height: int) -> None:
    """Validate a screenshot request before it ever reaches the EmberVM client.

    This is the SECOND of two layers (ADR embervm/035 section 4). The
    PRIMARY control is the in-guest proxy's hard allowlist, Go code running
    inside the Firecracker guest (issue #4994 T2), covered by its own Go
    test in that package: a Python check here cannot observe what the guest
    actually enforces. This layer exists so an obviously invalid request
    never reaches EmberVM at all, and so a caller gets an immediate, specific
    error instead of a guest-side failure several hops later.

    Raises InvalidShotterURL or InvalidViewportDimension on a bad request;
    returns None when the request is acceptable.
    """
    parsed = urlsplit(url)

    if parsed.scheme != "https":
        raise InvalidShotterURL(
            f"shotter only accepts the https scheme, got {parsed.scheme or '(none)'!r}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise InvalidShotterURL(
            "URL must not contain embedded credentials (userinfo before @)"
        )
    if parsed.port is not None:
        raise InvalidShotterURL(
            "URL must not specify an explicit port, expected the standard "
            f"HTTPS port, got {parsed.port}"
        )

    host = parsed.hostname or ""
    if host not in HOST_SERVICE_MAP:
        raise InvalidShotterURL(
            f"{host!r} is not allowed: not a recognized host in the shotter "
            f"allowlist, only {sorted(HOST_SERVICE_MAP)} are mapped"
        )

    _validate_dimension("width", width)
    _validate_dimension("height", height)


def _validate_dimension(name: str, value: int) -> None:
    if value <= 0:
        raise InvalidViewportDimension(
            f"{name} must be a positive, non-zero value, got {value}"
        )
    if value > _MAX_DIMENSION:
        raise InvalidViewportDimension(
            f"{name} {value} exceeds the max of {_MAX_DIMENSION}"
        )


async def screenshot_url(
    url: str,
    width: int = _DEFAULT_WIDTH,
    height: int = _DEFAULT_HEIGHT,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
) -> ImageContent:
    """Render a jomcgi.dev or private.jomcgi.dev page and return it as an image.

    Only these two hosts are ever dispatched, an in-cluster mapping (ADR
    embervm/035), and both are always fetched over HTTPS with no port, no
    embedded credentials, and no query string trickery around scheme or
    host. The rendered viewport is bounded (default 1024x768, up to
    4096x4096) and the whole request is bounded by timeout_ms.

    Args:
        url: An https URL under jomcgi.dev or private.jomcgi.dev. Any other
            host, scheme, port, or embedded userinfo is rejected before
            dispatch.
        width: Viewport width in pixels, 1 to 4096. Defaults to 1024.
        height: Viewport height in pixels, 1 to 4096. Defaults to 768.
        timeout_ms: Overall render budget in milliseconds. Defaults to
            30000. A page that does not finish loading in time surfaces as a
            timeout error rather than a partial or corrupt image.

    Returns:
        An image content block: the rendered PNG, embedded directly, plus
        metadata with the page's final URL after any redirect, the HTTP
        status the page returned, the viewport actually used, and a stored
        URL where the same PNG is archived for later reference.
    """
    validate_screenshot_url(url, width, height)

    result = await client.capture(
        url=url, width=width, height=height, timeout_ms=timeout_ms
    )

    png_b64 = result["png_b64"]
    png_bytes = base64.b64decode(png_b64)
    stored_url, stored = s3.put_screenshot(png_bytes)

    return ImageContent(
        type="image",
        data=png_b64,
        mimeType="image/png",
        _meta={
            "url": stored_url,
            # Whether the object behind `url` actually exists. The upload is
            # best-effort so a SeaweedFS blip does not throw away a screenshot
            # the caller can already see inline, but a URL that 404s must not be
            # indistinguishable from one that works.
            "stored": stored,
            "final_url": result.get("final_url"),
            "status": result.get("status"),
            "width": result.get("width"),
            "height": result.get("height"),
        },
    )


def register_mcp_tools() -> None:
    """Register the shotter screenshot tool with the shared FastMCP instance."""
    mcp.tool(name="screenshot_jomcgi_dev")(screenshot_url)
