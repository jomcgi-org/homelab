"""Draw the SVG figures for the oom-inference docs.

Run from anywhere: `python3 projects/oom-inference/docs/figures/build_figures.py`.
Each function is one figure; edit the function and regenerate, never the SVG.

The house style follows the blog's figures (docs/posts/figures/ at the repo
root): mono labels, outlines partitioned edge to edge, numbered callouts
explained by a `Key | Part` table under the figure. Unlike the blog, these SVGs
are shown by GitHub as images, where `currentColor` does not follow the page
theme, so every color is explicit and the figure has an opaque light
background: it reads the same in light and dark mode.
"""

from __future__ import annotations

import math
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent

MONO = "ui-monospace, SF Mono, Cascadia Mono, Menlo, monospace"
BG = "#ffffff"
INK = "#1f2328"
MUTED = "#59636e"
ACCENT = "#0969da"
OUTLINE = 1.25
LEADER = 1
CALLOUT_R = 9


class Figure:
    def __init__(self, width: int, height: int, title: str) -> None:
        self.width, self.height, self.title = width, height, title
        self.parts: list[str] = [
            f'<rect x="0" y="0" width="{width}" height="{height}" fill="{BG}"/>'
        ]

    def box(self, x, y, w, h, *, dashed=False, color=INK) -> None:
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="none" '
            f'stroke="{color}" stroke-width="{OUTLINE}"{dash}/>'
        )

    def text(self, x, y, s, *, anchor="start", size=11, color=INK, bold=False):
        fw = ' font-weight="bold"' if bold else ""
        self.parts.append(
            f'<text x="{x}" y="{y}" font-family="{MONO}" font-size="{size}" '
            f'fill="{color}" text-anchor="{anchor}"{fw}>{escape(s)}</text>'
        )

    def lines(self, x, y, rows, *, color=INK, step=14) -> None:
        for i, row in enumerate(rows):
            self.text(x, y + i * step, row, color=color)

    def line(self, x1, y1, x2, y2, *, color=INK, weight=LEADER, dashed=False):
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        self.parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
            f'stroke-width="{weight}"{dash}/>'
        )

    def hline(self, x1, x2, y, *, color=INK) -> None:
        self.line(x1, y, x2, y, color=color, weight=OUTLINE)

    def vline(self, x, y1, y2, *, color=INK) -> None:
        self.line(x, y1, x, y2, color=color, weight=OUTLINE)

    def head(self, x, y, ang, *, color=INK, size=5) -> None:
        for da in (math.pi * 0.8, -math.pi * 0.8):
            self.line(
                x,
                y,
                x + size * math.cos(ang + da),
                y + size * math.sin(ang + da),
                color=color,
            )

    def arrow(self, x1, y1, x2, y2, *, color=INK, dashed=False) -> None:
        self.line(x1, y1, x2, y2, color=color, dashed=dashed)
        self.head(x2, y2, math.atan2(y2 - y1, x2 - x1), color=color)

    def path_arrow(self, pts, *, color=INK) -> None:
        p = " ".join(f"{x},{y}" for x, y in pts)
        self.parts.append(
            f'<polyline points="{p}" fill="none" stroke="{color}" '
            f'stroke-width="{LEADER}"/>'
        )
        (x1, y1), (x2, y2) = pts[-2], pts[-1]
        self.head(x2, y2, math.atan2(y2 - y1, x2 - x1), color=color)

    def keyed(self, cx, cy, label, px, py) -> None:
        """A numbered callout with a leader to the part at (px, py)."""
        self.parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{CALLOUT_R}" fill="{BG}" '
            f'stroke="{INK}" stroke-width="{LEADER}"/>'
        )
        self.text(cx, cy + 4, label, anchor="middle")
        ang = math.atan2(py - cy, px - cx)
        self.line(
            cx + CALLOUT_R * math.cos(ang), cy + CALLOUT_R * math.sin(ang), px, py
        )
        self.parts.append(f'<circle cx="{px}" cy="{py}" r="2" fill="{INK}"/>')

    def svg(self) -> str:
        body = "\n  ".join(self.parts)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} '
            f'{self.height}" width="{self.width}" role="img" '
            f'aria-label="{escape(self.title)}">\n  <title>{escape(self.title)}'
            f"</title>\n  {body}\n</svg>\n"
        )


def memory_paths() -> Figure:
    """Where weights live and how expert records and PLE rows reach the GPU.

    Adapted from the blog's `oom_memory_paths` (walkthrough branch)."""
    f = Figure(720, 566, "Where the weights live and how they reach the GPU")

    # 1 GPU
    f.box(90, 26, 540, 104)
    f.text(102, 46, "GPU VRAM", bold=True)
    f.hline(90, 630, 56)
    f.vline(330, 56, 130)
    f.lines(102, 78, ["dense weights", "KV cache, sequence state", "prefill stage"])
    f.lines(
        342,
        78,
        ["VRAM expert tier", "record slots in chunks", "(what is left over)"],
        color=ACCENT,
    )
    f.keyed(38, 76, "1", 90, 76)

    # PCIe copies, arena to VRAM
    f.arrow(235, 206, 235, 138)
    f.text(247, 169, "PCIe copies")

    # 2 pinned host arena
    f.box(90, 212, 290, 80)
    f.text(102, 233, "PINNED HOST ARENA", bold=True)
    f.hline(90, 380, 244)
    f.lines(102, 264, ["host expert tier", "+ prefill staging ring"], color=ACCENT)
    f.keyed(38, 252, "2", 90, 252)

    # 4 CPU experts: reads the arena, sends output rows up
    f.box(440, 212, 140, 80)
    f.text(452, 233, "CPU EXPERTS", bold=True)
    f.hline(440, 580, 244)
    f.lines(452, 264, ["host-tier hits", "decode steps"])
    f.arrow(386, 260, 434, 260)
    f.arrow(510, 206, 510, 138)
    f.lines(522, 165, ["output", "rows"])
    f.keyed(612, 252, "4", 580, 252)

    # Direct reads, NVMe to arena
    f.arrow(235, 464, 235, 298)
    f.lines(102, 342, ["direct reads", "O_DIRECT", "no page cache"], color=MUTED)

    # 5 PLE row buffers
    f.box(440, 346, 190, 76)
    f.text(452, 367, "PLE ROW BUFFERS", bold=True)
    f.hline(440, 630, 378)
    f.lines(452, 398, ["rows a step needs", "page cache may hit"])
    f.path_arrow([(636, 392), (660, 392), (660, 94), (636, 94)])
    f.text(654, 328, "upload", anchor="end")
    f.keyed(692, 440, "5", 630, 410)
    f.arrow(520, 464, 520, 428)
    f.text(532, 449, "row reads")

    # 3 NVMe
    f.box(90, 470, 540, 76)
    f.text(102, 491, "NVMe: CONVERTED MODEL", bold=True)
    f.hline(90, 630, 502)
    f.vline(400, 502, 546)
    f.lines(102, 522, ["experts.bin", "one record per expert"])
    f.lines(412, 522, ["tables.bin", "n-gram (PLE) rows"])
    f.keyed(38, 508, "3", 90, 508)
    return f


def main() -> None:
    for name, build in {"memory-paths": memory_paths}.items():
        (HERE / f"{name}.svg").write_text(build().svg(), encoding="utf-8")


if __name__ == "__main__":
    main()
