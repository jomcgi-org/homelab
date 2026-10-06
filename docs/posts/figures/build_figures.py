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

from figlib import OUTLINE, Figure

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


def oom_memory_paths() -> Figure:
    f = Figure(720, 566, "Expert records and PLE rows take different paths")

    # The expert record path shares a vertical assembly axis. PLE rows use
    # their own buffered path at the right; the CPU reads the pinned arena.
    f.box(90, 26, 540, 104)
    f.text(102, 46, "GPU: RTX 4090, 24 GB VRAM")
    f.hline(90, 630, 56)
    f.lines(
        102,
        78,
        [
            "compute + dense weights + sequence state",
            "expert cache uses remaining VRAM",
            "state growth releases expert-cache chunks",
        ],
    )
    f.keyed(38, 76, "1", 90, 76)

    f.arrow(235, 206, 235, 138)
    f.text(247, 169, "PCIe copies")

    f.box(90, 212, 290, 80)
    f.text(102, 233, "PINNED HOST ARENA")
    f.hline(90, 380, 244)
    f.lines(102, 264, ["bounded expert-record cache", "direct reads land here"])
    f.keyed(38, 252, "2", 90, 252)

    f.box(440, 212, 140, 80)
    f.text(452, 233, "CPU EXPERTS")
    f.hline(440, 580, 244)
    f.lines(452, 264, ["host hits", "decode only"])
    f.arrow(386, 260, 434, 260)
    f.arrow(510, 206, 510, 138)
    f.lines(522, 165, ["output", "rows"])
    f.keyed(612, 252, "4", 580, 252)

    f.arrow(235, 464, 235, 298)
    f.lines(102, 342, ["O_DIRECT", "io_uring", "no page cache"])

    f.box(440, 346, 190, 76)
    f.text(452, 367, "PLE HOST BUFFERS")
    f.hline(440, 630, 378)
    f.lines(452, 398, ["buffered row reads", "page cache may hit"])
    f.path_arrow([(636, 392), (660, 392), (660, 94), (636, 94)])
    f.text(654, 328, "upload", anchor="end")
    f.keyed(692, 382, "5", 630, 382)
    f.arrow(520, 464, 520, 428)
    f.text(532, 449, "io_uring")

    f.box(90, 470, 540, 76)
    f.text(102, 491, "NVMe: CONVERTED MODEL FILES")
    f.hline(90, 630, 502)
    f.vline(400, 502, 546)
    f.lines(102, 522, ["experts.bin", "aligned records + their scales"])
    f.lines(412, 522, ["tables.bin", "n-gram lookup rows"])
    f.keyed(38, 508, "3", 90, 508)
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


def oom_residency() -> Figure:
    f = Figure(720, 336, "A GPU-resident baseline against the three expert tiers")

    # Left: everything fits in VRAM.
    f.box(24, 40, 250, 176)
    f.text(36, 62, "GPU-RESIDENT BASELINE", size=12, weight="bold")
    f.hline(24, 274, 74)
    f.lines(
        36,
        98,
        ["dense weights", "sequence state", "every expert in VRAM"],
        step=20,
        size=12,
    )
    f.text(36, 192, "needs a GPU that holds", size=12)
    f.text(36, 208, "the whole model", size=12)
    f.keyed(149, 244, "1", 149, 216)

    # Right: three tiers.
    f.box(334, 24, 362, 100)
    f.text(346, 46, "VRAM: 24 GB", size=12, weight="bold")
    f.hline(334, 696, 56)
    f.lines(
        346,
        78,
        ["dense weights + sequence state", "hot expert cache (the rest of VRAM)"],
        step=20,
        size=12,
    )
    f.keyed(300, 70, "2", 334, 70)

    f.box(334, 168, 170, 64)
    f.text(346, 190, "PINNED RAM", size=12, weight="bold")
    f.hline(334, 504, 200)
    f.text(346, 220, "warm expert records", size=12)
    f.keyed(300, 200, "3", 334, 200)

    f.box(334, 272, 170, 44)
    f.text(346, 298, "NVMe: all experts", size=12, weight="bold")
    f.keyed(300, 294, "4", 334, 294)

    f.arrow(380, 168, 380, 130)
    f.text(392, 152, "PCIe copy", size=12)
    f.arrow(430, 272, 430, 238)
    f.text(442, 258, "miss: direct read", size=12)

    f.box(560, 168, 130, 64)
    f.text(572, 190, "CPU", size=12, weight="bold")
    f.hline(560, 690, 200)
    f.text(572, 220, "host hit, decode", size=12)
    f.arrow(504, 200, 554, 200)
    f.path_arrow([(628, 168), (628, 130)])
    f.text(640, 152, "10 KB out", size=12)
    f.keyed(709, 200, "5", 690, 200)
    return f


def oom_context_state() -> Figure:
    f = Figure(
        720,
        380,
        "Context state grows with attention layers and shrinks the expert cache",
    )

    f.text(
        44,
        34,
        "FOUR OF THE 48 DECODER LAYERS, REPEATED 12 TIMES",
        size=12,
        weight="bold",
    )
    for i, name in enumerate(["GDN", "GDN", "GDN", "QSA"]):
        x = 44 + i * 115
        f.box(x, 50, 100, 34, weight=2 if name == "QSA" else OUTLINE)
        f.text(x + 50, 72, name, anchor="middle", size=12, weight="bold")
        f.text(
            x + 50,
            106,
            "fixed state" if name == "GDN" else "history grows",
            anchor="middle",
            size=12,
        )
    f.text(520, 62, "GDN: recurrent state,", size=12)
    f.text(520, 78, "fixed size per layer", size=12)
    f.text(520, 98, "QSA: reads selected rows", size=12)
    f.text(520, 114, "but stores all of them", size=12)
    f.keyed(94, 140, "1", 94, 114)
    f.keyed(439, 140, "2", 439, 114)

    def bar(y, title, dense, state, caption):
        total, x0 = 480, 44
        f.text(x0, y - 8, title, size=12, weight="bold")
        f.box(x0, y, total, 40)
        d, s = total * dense, total * state
        f.vline(x0 + d, y, y + 40)
        f.vline(x0 + d + s, y, y + 40)
        f.text(x0 + d / 2, y + 24, "dense", anchor="middle", size=12)
        if state > 0.12:
            f.text(x0 + d + s / 2, y + 24, "state", anchor="middle", size=12)
        f.text(
            x0 + d + s + (total - d - s) / 2,
            y + 24,
            "expert slots",
            anchor="middle",
            size=12,
        )
        f.text(x0 + total + 14, y + 24, caption, size=12)

    bar(190, "SHORTER CONTEXT (schematic)", 0.25, 0.06, "more experts stay")
    bar(262, "LONGER CONTEXT (schematic)", 0.25, 0.32, "fewer slots left")
    f.keyed(20, 188, "3", 44, 200)
    f.keyed(20, 252, "4", 44, 272)

    f.arrow(284, 236, 284, 254)
    f.text(296, 250, "grows on demand", size=12)
    f.text(44, 336, "Shrinking a prompt does not refill the cache by itself:", size=12)
    f.text(
        44,
        352,
        "a rewind keeps its buffers, and freed slots still need warming.",
        size=12,
    )
    return f


def main() -> None:
    for name, build in {
        "moe-selection": moe_selection,
        "expert-paths": expert_paths,
        "conformance-loop": conformance_loop,
        "oom-memory-paths": oom_memory_paths,
        "oom-expert-routing": oom_expert_routing,
        "oom-residency": oom_residency,
        "oom-context-state": oom_context_state,
    }.items():
        (HERE / f"{name}.svg").write_text(build().svg(), encoding="utf-8")


if __name__ == "__main__":
    main()
