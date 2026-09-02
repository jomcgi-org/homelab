"""Primitives for the keyed workshop-manual figures under docs/posts/figures.

Every figure on the blog is a line drawing: currentColor strokes, mono labels,
numbered callouts for stages, lettered callouts for parts, and a fixed tone per
memory tier (GPU, host RAM, page cache, NVMe, hot set) so colour means the same
thing in every figure.
This module holds the vocabulary so every figure shares one geometry (callout
radius, leader weight, hatch pitch, arrowhead) and a new figure is a layout,
never a restyle. The output is plain SVG with presentation attributes only, so
it passes the generator's figure validator (no href, no url(), no script).

Coordinates are viewBox units. The blog renders a figure at a 44em measure,
so a 704-unit-wide viewBox displays at about 1:1 and an 11px label stays 11px.
"""

from __future__ import annotations

import math
from xml.sax.saxutils import escape

MONO = "ui-monospace, SF Mono, Cascadia Mono, Menlo, monospace"
ACCENT = "var(--accent-ink)"
# Tier tones, defined in technical-drawing.css for both schemes. A tone names
# the memory tier a part belongs to; it is never decoration.
TONES = {
    "gpu": "var(--tone-gpu)",
    "ram": "var(--tone-ram)",
    "cache": "var(--tone-cache)",
    "disk": "var(--tone-disk)",
    "hot": "var(--tone-hot)",
}


def _paint(accent: bool, tone: str | None) -> str:
    if tone:
        return TONES[tone]
    return ACCENT if accent else "currentColor"


OUTLINE = 1.25
LEADER = 1
CALLOUT_R = 9
DOT_R = 2


class Figure:
    def __init__(self, width: int, height: int, title: str) -> None:
        self.width = width
        self.height = height
        self.title = title
        self.parts: list[str] = []

    # ---- primitives -----------------------------------------------------

    def box(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        dashed: bool = False,
        accent: bool = False,
        tone: str | None = None,
        weight: float = OUTLINE,
    ) -> None:
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        stroke = _paint(accent, tone)
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="none" '
            f'stroke="{stroke}" stroke-width="{weight}"{dash}/>'
        )

    def text(
        self,
        x: float,
        y: float,
        s: str,
        *,
        anchor: str = "start",
        size: float = 11,
        accent: bool = False,
        tone: str | None = None,
        weight: str | None = None,
    ) -> None:
        fill = _paint(accent, tone)
        fw = f' font-weight="{weight}"' if weight else ""
        self.parts.append(
            f'<text x="{x}" y="{y}" font-family="{MONO}" font-size="{size}" '
            f'fill="{fill}" text-anchor="{anchor}"{fw}>{escape(s)}</text>'
        )

    def lines(
        self,
        x: float,
        y: float,
        rows: list[str],
        *,
        step: float = 14,
        anchor: str = "start",
        size: float = 11,
        accent: bool = False,
        tone: str | None = None,
    ) -> None:
        for i, row in enumerate(rows):
            self.text(
                x, y + i * step, row, anchor=anchor, size=size, accent=accent, tone=tone
            )

    def callout(
        self, cx: float, cy: float, label: str, *, tone: str | None = None
    ) -> None:
        stroke = _paint(False, tone)
        # data-key/data-tone let the blog renderer paint the matching key
        # table cell in the same circle and colour as the figure callout.
        data = f' data-key="{escape(label)}" data-tone="{tone}"' if tone else ""
        self.parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{CALLOUT_R}" fill="none" '
            f'stroke="{stroke}" stroke-width="{LEADER}"{data}/>'
        )
        self.text(cx, cy + 4, label, anchor="middle", size=11, tone=tone)

    def dot(self, x: float, y: float) -> None:
        self.parts.append(
            f'<circle cx="{x}" cy="{y}" r="{DOT_R}" fill="currentColor"/>'
        )

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        dashed: bool = False,
        weight: float = LEADER,
        accent: bool = False,
        tone: str | None = None,
    ) -> None:
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        stroke = _paint(accent, tone)
        self.parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{stroke}" stroke-width="{weight}"{dash}/>'
        )

    def polyline(
        self, points: list[tuple[float, float]], *, dashed: bool = False
    ) -> None:
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        pts = " ".join(f"{x},{y}" for x, y in points)
        self.parts.append(
            f'<polyline points="{pts}" fill="none" stroke="currentColor" '
            f'stroke-width="{LEADER}"{dash}/>'
        )

    def leader(self, x1: float, y1: float, x2: float, y2: float) -> None:
        """Callout-to-part leader: a 1px line ending in a 2px dot on the part."""
        self.line(x1, y1, x2, y2)
        self.dot(x2, y2)

    def keyed(
        self,
        cx: float,
        cy: float,
        label: str,
        px: float,
        py: float,
        *,
        tone: str | None = None,
    ) -> None:
        """A callout whose leader starts at the circle's edge, towards the part."""
        ang = math.atan2(py - cy, px - cx)
        self.callout(cx, cy, label, tone=tone)
        self.leader(
            cx + CALLOUT_R * math.cos(ang), cy + CALLOUT_R * math.sin(ang), px, py
        )

    def head(self, x: float, y: float, ang: float, *, size: float = 5) -> None:
        """Open arrowhead at (x, y) pointing along ang (radians)."""
        for da in (math.pi * 0.8, -math.pi * 0.8):
            self.line(
                x,
                y,
                x + size * math.cos(ang + da),
                y + size * math.sin(ang + da),
            )

    def arrow(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        dashed: bool = False,
        both: bool = False,
    ) -> None:
        self.line(x1, y1, x2, y2, dashed=dashed)
        self.head(x2, y2, math.atan2(y2 - y1, x2 - x1))
        if both:
            self.head(x1, y1, math.atan2(y1 - y2, x1 - x2))

    def path_arrow(
        self, points: list[tuple[float, float]], *, dashed: bool = False
    ) -> None:
        """An orthogonal arrow through the given points, head on the last leg."""
        self.polyline(points, dashed=dashed)
        (x1, y1), (x2, y2) = points[-2], points[-1]
        self.head(x2, y2, math.atan2(y2 - y1, x2 - x1))

    def hline(self, x1: float, x2: float, y: float, *, tone: str | None = None) -> None:
        """Horizontal partition inside an outline, edge to edge."""
        self.line(x1, y, x2, y, weight=OUTLINE, tone=tone)

    def vline(self, x: float, y1: float, y2: float, *, tone: str | None = None) -> None:
        """Vertical partition inside an outline, edge to edge."""
        self.line(x, y1, x, y2, weight=OUTLINE, tone=tone)

    def hatch(
        self, x: float, y: float, w: float, *, pitch: float = 8, depth: float = 7
    ) -> None:
        """Hatching above a horizontal edge: the manual's mark for a locked part."""
        n = int(w // pitch)
        for i in range(n + 1):
            hx = x + i * pitch
            self.line(hx, y, min(hx + depth, x + w), y - depth)

    def axis(self, x: float, y1: float, y2: float) -> None:
        """Exploded-view assembly axis."""
        self.line(x, y1, x, y2, dashed=True)

    def cells(self, x: float, y: float, w: float, h: float, n: int) -> None:
        """A row of n small squares, for tokens or expert ids."""
        for i in range(n):
            self.box(x + i * (w + 4), y, w, h, weight=LEADER)

    # ---- panels ---------------------------------------------------------

    def panel(self, x: float, y: float, n: int, title: str) -> None:
        """A stage's callout and title; the cell itself is drawn by grid()."""
        self.callout(x + 18, y + 18, str(n))
        self.text(x + 34, y + 22, title, size=11)

    def grid(self, w: float, h: float, cols: list[float], rows: list[float]) -> None:
        """One outline partitioned into cells by edge-to-edge lines, never
        separate boxes with dead space between them."""
        self.box(0, 0, w, h)
        for x in cols:
            self.vline(x, 0, h)
        for y in rows:
            self.hline(0, w, y)

    # ---- output ---------------------------------------------------------

    def svg(self) -> str:
        body = "\n  ".join(self.parts)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} {self.height}" '
            f'role="img" aria-label="{escape(self.title)}">\n  {body}\n</svg>\n'
        )
