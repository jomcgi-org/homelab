"""og-image: the first real EmberVM zip-lane function (Task 12, ADR agents/045).

A pure-Pillow Open Graph image generator: `?title=...&subtitle=...` renders a
1200x630 PNG suitable for a social share card. It is the dogfood consumer that
proves the whole FaaS registration path end to end with no hand-authored CR, and
its binary PNG response exercises the base64 body path plus the guest-header
Content-Type fidelity that PR-A landed.

This module is GUEST code: it is packed into a zip by ``register.py`` and imported
inside a disposable python-runtime microVM by the bootstrap shim (see
``projects/embervm/runtimes/python/shim.py``), which calls ``handle(event, context)``.
It is globbed into ``:monolith_backend`` only so its unit test can import it in
CI; nothing in the monolith runtime imports it.

Restore-safe contract (ADR embervm/002): the base is snapshotted once and
restored per invoke, so this handler reads NO wall-clock and draws NO entropy at
import or in the body. Its output is a pure function of (title, subtitle), so
every restored invoke of the same request renders identical bytes.

Dependency contract: the only non-stdlib import is Pillow (``PIL``), which is in
the runtime base's baked subset (``py3.12-pillow``); ``register.py`` declares
``PIL`` so the ingestion API's baked-set check (``faas/runtime.py``) passes.
Pillow is also a monolith pip dep (``@pip//pillow``), so the render is exercised
for real in CI, not just live.
"""

from __future__ import annotations

import base64
import io
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# Open Graph's canonical share-card size (the 1.91:1 ratio Facebook/Twitter/
# LinkedIn crop to). Fixed so the output is deterministic.
WIDTH = 1200
HEIGHT = 630

# Palette (mirrors the site's dark surface; no external theme dependency).
BG = (13, 17, 23)  # near-black slate
TITLE_FG = (233, 237, 243)  # off-white
SUBTITLE_FG = (139, 148, 158)  # muted grey
ACCENT = (56, 139, 253)  # the site's link blue, drawn as a top rule

MARGIN = 90  # left/right/top text inset
TITLE_SIZE = 84
SUBTITLE_SIZE = 44
BRAND_SIZE = 30

# Defensive input caps: a title/subtitle is drawn, never executed, but bounding
# the length keeps the render cheap and the layout sane. Truncated with an
# ellipsis so an over-long input degrades visibly rather than silently.
TITLE_MAX = 120
SUBTITLE_MAX = 200

BRAND = "jomcgi.dev"
DEFAULT_TITLE = "jomcgi.dev"


def _font(size: int) -> ImageFont.FreeTypeFont:
    """Return a scalable default font at ``size``.

    Pillow 10.1+ (we bake 12.x) ships a bundled TrueType face reachable via
    ``load_default(size=...)``, so the archive needs no font file and the render
    stays self-contained. This is deliberately not a system-font lookup: the
    result must be identical in CI and in the guest.
    """
    return ImageFont.load_default(size=size)


def _clamp(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters, appending an ellipsis if cut."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """Greedy word-wrap ``text`` to lines no wider than ``max_width`` pixels.

    A single word longer than ``max_width`` is left on its own line rather than
    hard-split, so the render never loops; the layout tolerates the overflow.
    """
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def render_png(title: str, subtitle: str) -> bytes:
    """Render the OG card to PNG bytes. Pure function of (title, subtitle)."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    # Accent rule across the top edge.
    draw.rectangle((0, 0, WIDTH, 10), fill=ACCENT)

    title_font = _font(TITLE_SIZE)
    subtitle_font = _font(SUBTITLE_SIZE)
    brand_font = _font(BRAND_SIZE)

    text_width = WIDTH - 2 * MARGIN
    title_lines = _wrap(draw, title, title_font, text_width)

    # Vertically anchor the title block a little above centre so the subtitle
    # and brand mark have room beneath it.
    line_gap = 16
    title_line_h = TITLE_SIZE + line_gap
    block_h = len(title_lines) * title_line_h
    y = max(MARGIN, (HEIGHT - block_h) // 2 - 40)
    for line in title_lines:
        draw.text((MARGIN, y), line, font=title_font, fill=TITLE_FG)
        y += title_line_h

    if subtitle:
        y += 8
        for line in _wrap(draw, subtitle, subtitle_font, text_width):
            draw.text((MARGIN, y), line, font=subtitle_font, fill=SUBTITLE_FG)
            y += SUBTITLE_SIZE + line_gap

    # Brand mark pinned to the bottom-left.
    draw.text(
        (MARGIN, HEIGHT - MARGIN),
        BRAND,
        font=brand_font,
        fill=SUBTITLE_FG,
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def handle(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Marshal the request into a PNG response in the shim's normative shape.

    Reads ``title``/``subtitle`` from ``queryStringParameters`` (the invocation
    router appends the caller's raw query to the guest path, so the shim parses
    it into that field). Returns the PNG base64-encoded with
    ``isBase64Encoded=True`` so the binary survives the JSON round-trip, and
    ``Content-Type: image/png`` so EmberVM's guest-header fidelity (PR-A) relays
    the type back to the caller.
    """
    params = event.get("queryStringParameters") or {}
    title = _clamp(params.get("title") or DEFAULT_TITLE, TITLE_MAX)
    subtitle = _clamp(params.get("subtitle") or "", SUBTITLE_MAX)

    png = render_png(title, subtitle)
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "image/png",
            # Share cards are immutable for a given query; let edges cache them.
            "Cache-Control": "public, max-age=86400",
        },
        "body": base64.b64encode(png).decode("ascii"),
        "isBase64Encoded": True,
    }
