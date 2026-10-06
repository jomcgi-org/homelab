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


def memory_tiers() -> Figure:
    f = Figure(700, 470, "Where the weights live")
    ax = 350
    # The assembly axis shows only in the gaps: parts occlude it, as in an
    # exploded view, so it never runs through a label.
    for y1, y2 in ((12, 28), (254, 280), (336, 366), (442, 458)):
        f.axis(ax, y1, y2)

    # 1 GPU
    f.box(150, 28, 400, 100, tone="gpu")
    f.text(160, 46, "GPU: RTX 4090, 24 GB VRAM", tone="gpu")
    f.hline(150, 550, 54, tone="gpu")
    f.vline(270, 54, 128, tone="gpu")
    f.vline(420, 54, 128, tone="gpu")
    f.lines(160, 74, ["dense layers", "9.2 GiB"])
    f.lines(
        280, 74, ["expert slot cache", "hot set stays", "resident here"], tone="hot"
    )
    f.text(430, 74, "KV cache")
    f.keyed(40, 78, "1", 150, 78, tone="hot")

    # 2 PCIe
    f.arrow(ax, 134, ax, 170, both=True)
    f.text(ax + 10, 156, "PCIe 4.0 x16")
    f.keyed(40, 152, "2", ax - 3, 152)

    # 3 pinned host memory
    f.hatch(150, 176, 400)
    f.box(150, 176, 400, 78, tone="ram")
    f.text(160, 194, "PINNED HOST MEMORY: part of the 64 GB DDR5", tone="ram")
    f.hline(150, 550, 202, tone="ram")
    f.vline(400, 202, 254, tone="ram")
    f.lines(160, 222, ["expert banks", "budgeted with page cache"], tone="warm")
    f.lines(410, 222, ["transfer buffers", "bounded staging"], tone="ram")
    f.keyed(40, 215, "3", 150, 215, tone="warm")

    # 6 CPU executor, fed from the page cache. Level with the pinned host
    # memory part: same top and bottom edges, so the CPU reads as the
    # other half of the host beside its memory.
    f.box(590, 176, 96, 78)
    f.lines(598, 203, ["CPU MoE", "executor", "host RAM"])
    f.keyed(638, 148, "6", 638, 176)
    f.path_arrow([(550, 308), (570, 308), (570, 236), (590, 236)])

    # 4 page cache
    f.box(150, 280, 400, 56, dashed=True, tone="cache")
    f.text(160, 298, "PAGE CACHE: shares the host's 64 GB with 3", tone="cache")
    f.text(160, 316, "file pages in RAM; a miss needs NVMe", tone="cold")
    f.keyed(40, 308, "4", 150, 308, tone="cold")

    # 5 NVMe
    f.box(150, 366, 400, 76, tone="disk")
    f.text(160, 384, "NVMe: 1.9 TB", tone="disk")
    f.hline(150, 550, 392, tone="disk")
    f.vline(400, 392, 442, tone="disk")
    f.lines(160, 412, ["expert banks, 63.5 GiB", "48 layers x 512 experts"])
    f.lines(410, 412, ["lookup table, 27 GiB", "n-gram rows"])
    f.keyed(40, 404, "5", 150, 404, tone="disk")
    return f


def _decode_parts(f: Figure, px: float, py: float, *, letters: bool) -> None:
    """The five parts of one decode panel, in the same place on every panel."""
    x = lambda v: px + v  # noqa: E731
    y = lambda v: py + v  # noqa: E731
    # A GPU
    f.box(x(48), y(40), 210, 62, tone="gpu")
    f.text(x(54), y(54), "GPU", tone="gpu")
    f.hline(x(48), x(258), y(60), tone="gpu")
    f.vline(x(128), y(60), y(102), tone="hot")
    f.vline(x(190), y(60), y(102), tone="hot")
    f.text(x(54), y(85), "slot cache")
    f.text(x(134), y(85), "hot set", tone="hot")
    f.text(x(198), y(85), "KV")
    # E CPU executor
    f.box(x(272), y(118), 56, 40)
    f.lines(x(278), y(134), ["CPU", "exec"])
    # B pinned banks
    f.box(x(48), y(118), 210, 40, tone="ram")
    f.lines(x(54), y(134), ["pinned banks", "host RAM"], tone="warm")
    # C page cache
    f.box(x(48), y(172), 210, 34, dashed=True, tone="cache")
    f.text(x(54), y(192), "cold experts: page cache", tone="cold")
    # D NVMe
    f.box(x(48), y(220), 210, 40, tone="disk")
    f.line(x(170), y(220), x(170), y(260), weight=1.25, tone="disk")
    f.lines(x(54), y(236), ["expert banks", "63.5 GiB"], tone="disk")
    f.lines(x(176), y(236), ["table", "27 GiB"], tone="disk")
    if letters:
        f.keyed(x(26), y(71), "A", x(48), y(71), tone="hot")
        f.keyed(x(26), y(138), "B", x(48), y(138), tone="warm")
        f.keyed(x(26), y(189), "C", x(48), y(189), tone="cold")
        f.keyed(x(26), y(240), "D", x(48), y(240), tone="disk")
        f.keyed(x(300), y(174), "E", x(300), y(158))


def decode_step() -> Figure:
    f = Figure(700, 632, "One decode step through one expert layer")
    slots = [(0, 0), (350, 0), (0, 316), (350, 316)]
    f.grid(700, 632, cols=[350], rows=[316])
    titles = ["Route", "Sort by residency", "Move", "Compute and tally"]
    for i, ((px, py), title) in enumerate(zip(slots, titles)):
        f.panel(px, py, i + 1, title)
        _decode_parts(f, px, py, letters=(i == 0))

    # 1 Route
    px, py = slots[0]
    f.text(px + 54, py + 112, "n-gram ids for this token: already known")
    f.text(px + 54, py + 282, "hidden state")
    f.arrow(px + 136, py + 278, px + 150, py + 278)
    f.box(px + 152, py + 268, 46, 20)
    f.text(px + 158, py + 282, "router")
    tall = {2, 5, 9, 13, 16, 20}
    for i in range(24):
        tx = px + 210 + i * 5
        h = 18 if i in tall else 8
        f.line(tx, py + 290, tx, py + 290 - h, weight=1.25 if i in tall else 1)
    f.text(px + 210, py + 304, "512 a layer, top-k")

    # 2 Sort by residency
    px, py = slots[1]
    rows = [
        ("hot", 6, "reuse A", "hot"),
        ("pinned", 2, "B to A on cache miss", "warm"),
        ("cold", 4, "C hit, or read D", "cold"),
    ]
    for i, (name, n, where, tone) in enumerate(rows):
        yy = py + 274 + i * 14
        f.text(px + 54, yy, name, tone=tone)
        f.cells(px + 104, yy - 8, 8, 8, n)
        f.text(px + 180, yy, where)

    # 3 Move
    px, py = slots[2]
    f.arrow(px + 90, py + 118, px + 90, py + 102)
    f.text(px + 96, py + 114, "PCIe")
    f.text(px + 140, py + 114, "hot: stays")
    f.arrow(px + 70, py + 220, px + 70, py + 206, dashed=True)
    f.text(px + 78, py + 216, "willneed")
    f.path_arrow([(px + 258, py + 189), (px + 300, py + 189), (px + 300, py + 158)])
    f.text(px + 306, py + 182, "read")
    f.path_arrow(
        [
            (px + 48, py + 182),
            (px + 38, py + 182),
            (px + 38, py + 96),
            (px + 48, py + 96),
        ]
    )
    f.text(px + 6, py + 140, "stage")
    f.text(px + 54, py + 282, "table rows: gather into pinned staging,")
    f.text(px + 54, py + 296, "then copy to GPU before the forward")

    # 4 Compute and tally
    px, py = slots[3]
    f.callout(px + 300, py + 88, "+")
    f.arrow(px + 258, py + 80, px + 291, py + 86)
    f.arrow(px + 300, py + 118, px + 300, py + 97)
    f.text(px + 312, py + 92, "out")
    heights = [26, 22, 18, 15, 12, 10, 8, 7, 6, 5, 4, 3]
    for i, h in enumerate(heights):
        bx = px + 54 + i * 8
        f.line(bx, py + 300, bx, py + 300 - h, weight=1.25)
    f.line(px + 50, py + 300, px + 150, py + 300)
    f.text(px + 160, py + 282, "per-expert counters")
    f.text(px + 160, py + 296, "decay with newer traffic")
    return f


def _strip(f: Figure, titles: list[str], ph: int = 250) -> list[tuple[float, float]]:
    """Three stages across one outline, divided by two partition lines."""
    pw = 233
    slots = [(i * pw, 0) for i in range(3)]
    f.grid(700, ph, cols=[pw, 2 * pw], rows=[])
    for i, ((px, py), title) in enumerate(zip(slots, titles)):
        f.panel(px, py, i + 1, title)
    return slots


def hot_set_swap() -> Figure:
    f = Figure(700, 250, "How one hot-set slot changes hands")
    slots = _strip(f, ["Tick", "Stage", "Flip"])
    for i, (px, py) in enumerate(slots):
        f.box(px + 28, py + 40, 116, 48, tone="gpu")
        f.text(px + 34, py + 54, "GPU slot cache", tone="gpu")
        f.hline(px + 28, px + 144, py + 60, tone="gpu")
        f.vline(px + 56, py + 60, py + 88, tone="hot")
        f.vline(px + 118, py + 60, py + 88, tone="hot")
        f.text(px + 62, py + 78, "hot slot", tone="hot")
        f.box(px + 156, py + 60, 52, 22, tone="ram")
        f.text(px + 160, py + 75, "staging", tone="ram")
        f.box(px + 28, py + 196, 180, 34, tone="disk")
        f.text(px + 34, py + 216, "NVMe expert banks", tone="disk")
        if i == 0:
            f.keyed(px + 14, py + 64, "A", px + 28, py + 64, tone="hot")
            f.keyed(px + 14, py + 213, "D", px + 28, py + 213, tone="disk")

    # 1 Tick
    px, py = slots[0]
    f.text(px + 34, py + 104, "re-ranked every 1,000 steps")
    heights = [60, 44, 34, 28, 24, 20, 17, 14, 12, 10, 8, 7]
    for i, h in enumerate(heights):
        bx = px + 34 + i * 12
        if i == 5:
            f.box(bx, py + 176 - h, 8, h, dashed=True)
        elif i == 6:
            f.box(bx, py + 176 - h, 8, h, tone="hot")
            f.arrow(bx + 4, py + 176 - h - 14, bx + 4, py + 176 - h - 3)
        else:
            f.box(bx, py + 176 - h, 8, h)
    f.line(px + 30, py + 176, px + 182, py + 176)
    f.line(px + 105, py + 112, px + 105, py + 180, dashed=True)
    f.text(px + 110, py + 122, "6 GB budget")

    # 2 Stage
    px, py = slots[1]
    f.arrow(px + 180, py + 196, px + 180, py + 84, dashed=True)
    f.text(px + 36, py + 104, "old row still serves")
    f.lines(px + 64, py + 130, ["background copy,", "0.5 GB a tick"])

    # 3 Flip
    px, py = slots[2]
    f.arrow(px + 156, py + 71, px + 138, py + 71)
    f.lines(px + 36, py + 104, ["mapping flips at a", "step boundary"])
    f.text(px + 36, py + 140, "retired slot: free")
    f.lines(px + 36, py + 166, ["hot rate, drifted traffic", "62.6% to 73.3%"])
    return f


def prefill_chunk() -> Figure:
    f = Figure(700, 270, "A prefill chunk: known before the forward, read once")
    slots = _strip(f, ["Before the forward", "One read each", "Forward"], ph=270)
    for i, (px, py) in enumerate(slots):
        f.box(px + 28, py + 40, 116, 44, tone="gpu")
        f.text(px + 34, py + 54, "GPU", tone="gpu")
        f.hline(px + 28, px + 144, py + 60, tone="gpu")
        f.text(px + 34, py + 76, "pinned bank")
        f.box(px + 156, py + 100, 52, 34)
        f.lines(px + 160, py + 116, ["CPU", "exec"])
        f.box(px + 28, py + 100, 116, 30, dashed=True, tone="cache")
        f.text(px + 34, py + 119, "page cache", tone="cache")
        f.box(px + 28, py + 216, 180, 34, tone="disk")
        f.line(px + 110, py + 216, px + 110, py + 250, weight=1.25, tone="disk")
        f.text(px + 34, py + 236, "table 27G", tone="disk")
        f.text(px + 116, py + 236, "banks 63.5G", tone="disk")
        if i == 0:
            f.keyed(px + 14, py + 62, "A", px + 28, py + 62, tone="gpu")
            f.keyed(px + 14, py + 115, "C", px + 28, py + 115, tone="cache")
            f.keyed(px + 14, py + 233, "D", px + 28, py + 233, tone="disk")
            f.keyed(px + 182, py + 86, "E", px + 182, py + 100)

    # 1 Before the forward
    px, py = slots[0]
    f.cells(px + 34, py + 146, 8, 8, 8)
    f.text(px + 34, py + 168, "2,048 input tokens")
    f.arrow(px + 160, py + 150, px + 160, py + 190)
    f.cells(px + 34, py + 194, 8, 8, 3)
    f.text(px + 76, py + 202, "n-gram ids, deduped")

    # 2 One read each
    px, py = slots[1]
    f.path_arrow(
        [
            (px + 60, py + 216),
            (px + 60, py + 208),
            (px + 20, py + 208),
            (px + 20, py + 68),
            (px + 28, py + 68),
        ]
    )
    f.lines(px + 34, py + 152, ["table rows:", "one coalesced", "read"])
    f.arrow(px + 130, py + 216, px + 130, py + 130)
    f.lines(px + 137, py + 152, ["expert rows:", "per layer,", "after routing"])

    # 3 Forward
    px, py = slots[2]
    f.arrow(px + 144, py + 115, px + 156, py + 115)
    f.text(px + 34, py + 152, "lookup by compact local id")
    f.text(px + 34, py + 166, "hot experts on the GPU", tone="hot")
    f.text(px + 34, py + 194, "faults a chunk: 3.6M to 5k")
    return f


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
    picks = [(3, 1), (7, 2), (11, 3), (12, 5), (14, 6), (17, 8), (19, 10), (22, 11), (26, 13), (29, 14)]
    for c, r in picks:
        f.box(px + c * pitch, py + r * pitch, cell, cell, weight=2)
    f.text(px, 48, "512 ROUTED EXPERTS IN THIS LAYER")
    f.path_arrow([(186, 116), (210, 116), (210, 134), (px - 4, 134)])
    f.keyed(640, 62, "2", px + 32 * pitch - 4, py + 4)

    f.arrow(px + 32 * pitch + 6, 134, 616, 134)
    f.box(616, 106, 90, 56)
    f.text(661, 130, "COMBINE", anchor="middle", weight="bold")
    f.text(661, 146, "outputs", anchor="middle")

    # Work every token does regardless of the route.
    f.box(232, 272, 352, 34, dashed=False)
    f.text(244, 293, "dense + shared-expert work: every token")
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
    f = Figure(720, 380, "Context state grows with attention layers and shrinks the expert cache")

    f.text(44, 34, "FOUR OF THE 48 DECODER LAYERS, REPEATED 12 TIMES", size=12, weight="bold")
    for i, name in enumerate(["GDN", "GDN", "GDN", "QSA"]):
        x = 44 + i * 115
        f.box(x, 50, 100, 34, weight=2 if name == "QSA" else OUTLINE)
        f.text(x + 50, 72, name, anchor="middle", size=12, weight="bold")
        f.text(x + 50, 106, "fixed state" if name == "GDN" else "history grows", anchor="middle", size=12)
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
        f.text(x0 + d + s + (total - d - s) / 2, y + 24, "expert slots", anchor="middle", size=12)
        f.text(x0 + total + 14, y + 24, caption, size=12)

    bar(190, "SHORTER CONTEXT (schematic)", 0.25, 0.06, "more experts stay")
    bar(262, "LONGER CONTEXT (schematic)", 0.25, 0.32, "fewer slots left")
    f.keyed(20, 188, "3", 44, 200)
    f.keyed(20, 252, "4", 44, 272)

    f.arrow(284, 236, 284, 254)
    f.text(296, 250, "grows on demand", size=12)
    f.text(44, 336, "Shrinking a prompt does not refill the cache by itself:", size=12)
    f.text(44, 352, "a rewind keeps its buffers, and freed slots still need warming.", size=12)
    return f


def main() -> None:
    for name, build in {
        "memory-tiers": memory_tiers,
        "decode-step": decode_step,
        "hot-set-swap": hot_set_swap,
        "prefill-chunk": prefill_chunk,
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
