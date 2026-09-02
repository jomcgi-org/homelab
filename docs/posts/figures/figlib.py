"""Primitives for the keyed workshop-manual figures under docs/posts/figures.

Every figure on the blog is a line drawing: currentColor strokes, mono labels,
numbered callouts for stages, lettered callouts for parts, one accent part.
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
        weight: float = OUTLINE,
    ) -> None:
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        stroke = ACCENT if accent else "currentColor"
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
        weight: str | None = None,
    ) -> None:
        fill = ACCENT if accent else "currentColor"
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
    ) -> None:
        for i, row in enumerate(rows):
            self.text(x, y + i * step, row, anchor=anchor, size=size, accent=accent)

    def callout(self, cx: float, cy: float, label: str) -> None:
        self.parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{CALLOUT_R}" fill="none" '
            f'stroke="currentColor" stroke-width="{LEADER}"/>'
        )
        self.text(cx, cy + 4, label, anchor="middle", size=11)

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
    ) -> None:
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        stroke = ACCENT if accent else "currentColor"
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

    def keyed(self, cx: float, cy: float, label: str, px: float, py: float) -> None:
        """A callout whose leader starts at the circle's edge, towards the part."""
        ang = math.atan2(py - cy, px - cx)
        self.callout(cx, cy, label)
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

    def panel(self, x: float, y: float, w: float, h: float, n: int, title: str) -> None:
        self.box(x, y, w, h)
        self.callout(x + 18, y + 18, str(n))
        self.text(x + 34, y + 22, title, size=11)

    # ---- output ---------------------------------------------------------

    def svg(self) -> str:
        body = "\n  ".join(self.parts)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} {self.height}" '
            f'role="img" aria-label="{escape(self.title)}">\n  {body}\n</svg>\n'
        )
