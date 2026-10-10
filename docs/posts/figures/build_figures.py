"""Draw the keyed figures for docs/posts from figlib primitives.

Run from anywhere: `python3 docs/posts/figures/build_figures.py`. Each
function is one figure; the layout numbers are the drawing, so a change to a
figure is a change here, and the SVG is regenerated rather than edited.

Static figures and their numbered or lettered reference keys are monochrome.
Hardware, expert labels, and transfer arrows stay neutral. Red/yellow/blue
remain reserved for the replay's explicit hot/warm/cold routing display.

A part is one outline partitioned by lines that run edge to edge (a title
band, then columns), never a box drawn inside a box.
"""

from __future__ import annotations

from pathlib import Path

from figlib import Figure

HERE = Path(__file__).resolve().parent


def oom_expert_routing() -> Figure:
    f = Figure(720, 320, "One layer: 10 of 512 routed experts run for a token")

    f.text(18, 120, "token")
    f.arrow(58, 116, 90, 116)
    f.box(90, 86, 96, 60)
    f.text(138, 112, "ROUTER", anchor="middle", weight="bold")
    f.text(138, 130, "pick 10", anchor="middle")
    f.keyed(138, 58, "1", 138, 86)

    # The pool is 32 x 16 = 512 cells. Idle cells are one faint path; the ten
    # selected ones are drawn heavier on top (positions are illustrative).
    px, py, pitch, cell = 232, 62, 11, 8
    idle = "".join(
        f"M{px + c * pitch} {py + r * pitch}h{cell}v{cell}h-{cell}z"
        for r in range(16)
        for c in range(32)
    )
    f.parts.append(
        f'<path d="{idle}" fill="none" stroke="currentColor" stroke-width="1" '
        'stroke-opacity="0.3"/>'
    )
    picks = [
        (3, 1),
        (7, 2),
        (11, 3),
        (12, 5),
        (14, 6),
        (17, 8),
        (19, 10),
        (22, 11),
        (26, 13),
        (29, 14),
    ]
    for c, r in picks:
        f.box(px + c * pitch, py + r * pitch, cell, cell, weight=2)
    f.text(px, 48, "512 ROUTED EXPERTS IN THIS LAYER")
    f.path_arrow([(186, 116), (210, 116), (210, 134), (px - 4, 134)])
    f.keyed(640, 62, "2", px + 32 * pitch - 4, py + 4)

    f.arrow(px + 32 * pitch + 6, 134, 616, 134)
    f.box(616, 106, 90, 56)
    f.text(661, 130, "COMBINE", anchor="middle", weight="bold")
    f.text(661, 146, "outputs", anchor="middle")

    # The shared expert runs whatever the route and joins the combine.
    f.box(232, 272, 352, 34, dashed=False)
    f.text(244, 293, "shared expert: runs for every token")
    f.path_arrow([(584, 289), (661, 289), (661, 168)])
    f.keyed(206, 289, "3", 232, 289)

    f.text(18, 218, "heavy = routed", size=11)
    f.text(18, 234, "faint = idle", size=11)
    f.text(18, 250, "for this token", size=11)
    return f


def main() -> None:
    for name, build in {
        "oom-expert-routing": oom_expert_routing,
    }.items():
        (HERE / f"{name}.svg").write_text(build().svg(), encoding="utf-8")


if __name__ == "__main__":
    main()
