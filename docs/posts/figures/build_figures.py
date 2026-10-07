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


def moe_selection() -> Figure:
    f = Figure(700, 250, "One MoE layer: route, run selected experts, combine")
    f.text(18, 125, "input")
    f.arrow(58, 121, 95, 121)
    f.box(95, 96, 100, 50)
    f.text(145, 117, "ROUTER", anchor="middle", weight="bold")
    f.text(145, 133, "pick + weight", anchor="middle")
    f.text(330, 44, "EXPERT POOL", anchor="middle")
    for i in range(5):
        y = 58 + 31 * i
        selected = i in (1, 3)
        f.box(285, y, 90, 23, dashed=not selected, weight=2 if selected else 1)
        f.text(330, y + 16, f"expert {i + 1}", anchor="middle")
        if selected:
            f.path_arrow([(195, 121), (235, 121), (235, y + 11), (285, y + 11)])
            f.path_arrow([(375, y + 11), (420, y + 11), (420, 121), (465, 121)])
    f.box(465, 96, 112, 50)
    f.text(521, 117, "WEIGHTED SUM", anchor="middle", weight="bold")
    f.text(521, 133, "combine outputs", anchor="middle")
    f.arrow(577, 121, 630, 121)
    f.text(640, 125, "output")
    f.keyed(145, 174, "1", 145, 146)
    f.keyed(245, 44, "2", 285, 58)
    f.keyed(521, 174, "3", 521, 146)
    f.text(
        330,
        225,
        "illustration: 2 of 5 selected; dashed experts are idle",
        anchor="middle",
    )
    return f


def expert_paths() -> Figure:
    f = Figure(700, 256, "Expert transfer vs bandwidth")
    # Show transfer paths only; the memory figure owns capacities and budgets.
    f.box(32, 24, 340, 76)
    f.text(44, 43, "GPU", weight="bold")
    f.hline(32, 372, 52)
    f.parts.append('<g dominant-baseline="central">')
    # Optical offset aligns painted glyphs, rather than the font's em box.
    f.text(46, 77.35, "HOT experts")
    f.text(285, 77.35, "compute")
    f.parts.append("</g>")
    f.arrow(144, 78, 273, 78)
    f.text(209, 69, "1 TB/s", anchor="middle")

    f.box(32, 181, 170, 51)
    f.text(46, 200, "WARM experts", weight="bold")
    f.text(46, 220, "Pinned RAM")
    f.path_arrow([(117, 181), (117, 134), (316, 134), (316, 100)])
    f.text(211, 126, "PCIe / 25 GB/s", anchor="middle")

    f.box(526, 24, 142, 76)
    f.text(538, 43, "CPU", weight="bold")
    f.hline(526, 668, 52)
    f.text(597, 82, "compute", anchor="middle")
    f.box(308, 181, 146, 51)
    f.text(322, 200, "COLD experts", weight="bold")
    f.text(322, 220, "Page cache")
    f.path_arrow([(381, 181), (381, 134), (597, 134), (597, 100)])
    f.text(475, 126, "RAM speed", anchor="middle")
    f.box(554, 181, 114, 51)
    f.text(611, 211, "NVMe", anchor="middle", weight="bold")
    f.arrow(554, 207, 454, 207)
    f.text(504, 196, "7 GB/s", anchor="middle")
    f.text(504, 225, "on miss", anchor="middle")
    return f


def conformance_loop() -> Figure:
    """Exploded view of the path a chart takes from merge to production.

    Parts sit on one assembly axis: the merge at the top, the control plane
    and the checker in the middle, Kargo at the bottom. The runner stands to
    the right because it both drives the control plane and reads the checker,
    and its verdict is what Kargo polls."""
    f = Figure(700, 604, "The conformance loop from merge to promotion")
    ax = 240
    for y1, y2 in ((84, 112), (222, 250), (360, 470), (540, 572)):
        f.axis(ax, y1, y2)

    # 1 Merge: chart published, synced to dev, rollout awaited.
    f.box(40, 28, 620, 56)
    f.text(50, 46, "MERGE TO MAIN: chart version published")
    f.hline(40, 660, 54)
    f.vline(246, 54, 84)
    f.vline(453, 54, 84)
    f.text(50, 72, "ArgoCD syncs embervm-dev")
    f.text(256, 72, "Kargo waits for Healthy")
    f.text(463, 72, "runner sees the new version")
    f.keyed(20, 56, "1", 40, 56)

    # 3 Control plane: the thing under test, and the trace it writes.
    f.box(40, 112, 400, 110)
    f.text(50, 130, "CONTROL PLANE: Elixir, one per cluster")
    f.hline(40, 440, 138)
    f.vline(220, 138, 222)
    f.lines(50, 158, ["dispatcher", "session manager", "node registry"])
    f.lines(230, 158, ["SpecTrace writer", "15 event kinds", "checkpoint every 5 s"])
    f.hline(220, 440, 198)
    f.text(230, 214, "store: {run_id, seq, action, vars}")
    f.keyed(20, 167, "3", 40, 167)

    # 2 Runner: drives the scenarios and folds them into one verdict.
    f.box(480, 112, 180, 300)
    f.text(490, 130, "RUNNER: every 30 min")
    f.hline(480, 660, 138)
    rows = [
        "S1 two clones, vsock",
        "S2 sleep and relight",
        "S3 second start time",
        "S4 invariants",
        "S5 guest round trip",
    ]
    for i, row in enumerate(rows):
        y = 138 + i * 40
        f.text(490, y + 24, row)
        f.hline(480, 660, y + 40)
    f.text(490, 378, "GET /verdict")
    f.lines(490, 393, ["chart version, verdict,", "previous verdict"], size=10, step=12)
    f.keyed(570, 94, "2", 570, 112)
    f.arrow(480, 150, 440, 150)
    f.text(474, 142, "drives", anchor="end", size=10)

    # 4 Checker: nine invariants, three verdict values.
    f.box(40, 250, 400, 110)
    f.text(50, 268, "CHECKER: 9 invariants from adoption.tla")
    f.hline(40, 440, 276)
    f.vline(173, 276, 328)
    f.vline(306, 276, 328)
    f.text(106, 306, "pass", anchor="middle")
    f.text(239, 306, "fail", anchor="middle")
    f.text(373, 306, "vacuous", anchor="middle")
    f.hline(40, 440, 328)
    f.text(50, 348, "GET /v1/conformance: one verdict per invariant")
    f.keyed(20, 305, "4", 40, 305)
    f.arrow(ax, 222, ax, 250)
    f.text(ax + 8, 240, "trace window", size=10)
    f.arrow(440, 318, 480, 318)
    f.text(474, 332, "S4 reads", anchor="end", size=10)

    # 5 The runner's verdict is what Kargo polls.
    f.arrow(570, 412, 570, 470)
    f.keyed(610, 441, "5", 570, 441)

    # 6 Kargo: four rules, one of which promotes.
    f.box(40, 470, 620, 70)
    f.text(50, 488, "KARGO dev stage: polls /verdict for up to 75 min")
    f.hline(40, 660, 496)
    f.vline(246, 496, 540)
    f.vline(453, 496, 540)
    f.lines(50, 514, ["pass for this version:", "promote"])
    f.lines(256, 514, ["fail after a pass:", "hold, keep polling"])
    f.lines(463, 514, ["fail after a fail, or stale:", "promotion fails"])
    f.keyed(20, 505, "6", 40, 505)

    # Production, after the soak.
    f.box(140, 572, 200, 32)
    f.text(ax, 592, "PRODUCTION, after a 5 min soak", anchor="middle")
    return f


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
        "moe-selection": moe_selection,
        "expert-paths": expert_paths,
        "conformance-loop": conformance_loop,
        "oom-expert-routing": oom_expert_routing,
    }.items():
        (HERE / f"{name}.svg").write_text(build().svg(), encoding="utf-8")


if __name__ == "__main__":
    main()
