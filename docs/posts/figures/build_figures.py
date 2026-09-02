"""Draw the keyed figures for docs/posts from figlib primitives.

Run from anywhere: `python3 docs/posts/figures/build_figures.py`. Each
function is one figure; the layout numbers are the drawing, so a change to a
figure is a change here, and the SVG is regenerated rather than edited.
"""

from __future__ import annotations

from pathlib import Path

from figlib import Figure

HERE = Path(__file__).resolve().parent


def memory_tiers() -> Figure:
    f = Figure(700, 470, "Where the weights live")
    ax = 350
    # The assembly axis shows only in the gaps: parts occlude it, as in an
    # exploded view, so it never runs through a label.
    for y1, y2 in ((12, 28), (254, 280), (336, 366), (442, 458)):
        f.axis(ax, y1, y2)

    # 1 GPU
    f.box(150, 28, 400, 100)
    f.text(160, 46, "GPU: RTX 4090, 24 GB VRAM")
    f.box(160, 58, 110, 56)
    f.lines(168, 76, ["dense layers", "attention, GDN"])
    f.box(280, 58, 150, 56)
    f.lines(288, 76, ["expert slot cache", "hot set stays", "resident here"])
    f.box(440, 58, 100, 56)
    f.text(448, 76, "KV cache")
    f.keyed(40, 78, "1", 150, 78)

    # 2 PCIe
    f.arrow(ax, 134, ax, 170, both=True)
    f.text(ax + 10, 156, "PCIe 4.0 x16")
    f.keyed(40, 152, "2", ax - 3, 152)

    # 3 pinned host memory
    f.hatch(150, 176, 400)
    f.box(150, 176, 400, 78)
    f.text(160, 194, "PINNED HOST MEMORY: part of the 64 GB DDR5")
    f.box(160, 204, 240, 42)
    f.lines(168, 221, ["expert banks, 40 GB", "30 whole layers"])
    f.box(410, 204, 130, 42, accent=True)
    f.lines(418, 221, ["hot rows, 6 GiB", "18 disk layers"], accent=True)
    f.keyed(40, 215, "3", 150, 215)

    # 6 CPU executor, fed from the page cache
    f.box(590, 196, 96, 58)
    f.lines(598, 214, ["CPU MoE", "executor", "8 cores"])
    f.keyed(638, 166, "6", 638, 196)
    f.path_arrow([(550, 308), (570, 308), (570, 236), (590, 236)])

    # 4 page cache
    f.box(150, 280, 400, 56, dashed=True)
    f.text(160, 298, "PAGE CACHE: about 16 GB, what the kernel keeps of 5")
    f.text(160, 316, "recently used cold expert rows and table rows")
    f.keyed(40, 308, "4", 150, 308)

    # 5 NVMe
    f.box(150, 366, 400, 76)
    f.text(160, 384, "NVMe: 1.9 TB")
    f.box(160, 394, 230, 40)
    f.lines(168, 411, ["expert banks, 72.7 GiB", "48 layers x 512 experts"])
    f.box(400, 394, 140, 40)
    f.lines(408, 411, ["lookup table, 27 GiB", "n-gram rows"])
    f.keyed(40, 404, "5", 150, 404)
    return f


def _decode_parts(f: Figure, px: float, py: float, *, letters: bool) -> None:
    """The five parts of one decode panel, in the same place on every panel."""
    x = lambda v: px + v  # noqa: E731
    y = lambda v: py + v  # noqa: E731
    # A GPU
    f.box(x(48), y(40), 210, 62)
    f.text(x(54), y(54), "GPU")
    f.box(x(54), y(60), 74, 34)
    f.text(x(58), y(80), "slot cache")
    f.box(x(134), y(60), 54, 34, accent=True)
    f.text(x(140), y(80), "hot set", accent=True)
    f.box(x(194), y(60), 58, 34)
    f.text(x(200), y(80), "KV")
    # E CPU executor
    f.box(x(272), y(118), 56, 40)
    f.lines(x(278), y(134), ["CPU", "exec"])
    # B pinned banks
    f.box(x(48), y(118), 210, 40)
    f.lines(x(54), y(134), ["pinned banks", "40 GB"])
    # C page cache
    f.box(x(48), y(172), 210, 34, dashed=True)
    f.text(x(54), y(192), "page cache")
    # D NVMe
    f.box(x(48), y(220), 210, 40)
    f.line(x(170), y(220), x(170), y(260), weight=1.25)
    f.lines(x(54), y(236), ["expert banks", "72.7 GiB"])
    f.lines(x(176), y(236), ["table", "27 GiB"])
    if letters:
        f.keyed(x(26), y(71), "A", x(48), y(71))
        f.keyed(x(26), y(138), "B", x(48), y(138))
        f.keyed(x(26), y(189), "C", x(48), y(189))
        f.keyed(x(26), y(240), "D", x(48), y(240))
        f.keyed(x(300), y(174), "E", x(300), y(158))


def decode_step() -> Figure:
    f = Figure(700, 632, "One decode step through one expert layer")
    pw, ph = 342, 308
    slots = [(0, 0), (358, 0), (0, 324), (358, 324)]
    titles = ["Route", "Sort by residency", "Move", "Compute and tally"]
    for i, ((px, py), title) in enumerate(zip(slots, titles)):
        f.panel(px, py, pw, ph, i + 1, title)
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
        ("hot", 6, "stay on A (72%)"),
        ("pinned", 2, "fetched from B"),
        ("cold", 4, "read from D through C"),
    ]
    for i, (name, n, where) in enumerate(rows):
        yy = py + 274 + i * 14
        f.text(px + 54, yy, name)
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
    f.text(px + 6, py + 140, "HMM")
    f.text(px + 54, py + 282, "table rows: the GPU reads the file mapping")
    f.text(px + 54, py + 296, "itself, through the page cache")

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
    f.text(px + 160, py + 296, "half-life 2,000 steps")
    return f


def _strip(f: Figure, titles: list[str], ph: int = 250) -> list[tuple[float, float]]:
    pw, gap = 224, 14
    slots = [(i * (pw + gap), 0) for i in range(3)]
    for i, ((px, py), title) in enumerate(zip(slots, titles)):
        f.panel(px, py, pw, ph, i + 1, title)
    return slots


def hot_set_swap() -> Figure:
    f = Figure(700, 250, "How one hot-set slot changes hands")
    slots = _strip(f, ["Tick", "Stage", "Flip"])
    for i, (px, py) in enumerate(slots):
        f.box(px + 28, py + 40, 116, 48)
        f.text(px + 34, py + 54, "GPU slot cache")
        f.box(px + 36, py + 60, 100, 22, accent=True)
        f.text(px + 42, py + 75, "hot slot", accent=True)
        f.box(px + 156, py + 60, 52, 22)
        f.text(px + 160, py + 75, "staging")
        f.box(px + 28, py + 196, 180, 34)
        f.text(px + 34, py + 216, "NVMe expert banks")
        if i == 0:
            f.keyed(px + 14, py + 64, "A", px + 28, py + 64)
            f.keyed(px + 14, py + 213, "D", px + 28, py + 213)

    # 1 Tick
    px, py = slots[0]
    f.text(px + 34, py + 104, "re-ranked every 1,000 steps")
    heights = [60, 44, 34, 28, 24, 20, 17, 14, 12, 10, 8, 7]
    for i, h in enumerate(heights):
        bx = px + 34 + i * 12
        if i == 5:
            f.box(bx, py + 176 - h, 8, h, dashed=True)
        elif i == 6:
            f.box(bx, py + 176 - h, 8, h, accent=True)
            f.arrow(bx + 4, py + 176 - h - 14, bx + 4, py + 176 - h - 3)
        else:
            f.box(bx, py + 176 - h, 8, h)
    f.line(px + 30, py + 176, px + 182, py + 176)
    f.line(px + 105, py + 112, px + 105, py + 180, dashed=True)
    f.text(px + 110, py + 122, "6 GiB budget")

    # 2 Stage
    px, py = slots[1]
    f.arrow(px + 180, py + 196, px + 180, py + 84, dashed=True)
    f.text(px + 36, py + 104, "old row still serves")
    f.lines(px + 64, py + 130, ["background copy,", "0.5 GiB a tick"])

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
        f.box(px + 28, py + 40, 116, 44)
        f.text(px + 34, py + 54, "GPU")
        f.box(px + 36, py + 58, 100, 20, accent=True)
        f.text(px + 42, py + 72, "pinned bank", accent=True)
        f.box(px + 156, py + 100, 52, 34)
        f.lines(px + 160, py + 116, ["CPU", "exec"])
        f.box(px + 28, py + 100, 116, 30, dashed=True)
        f.text(px + 34, py + 119, "page cache")
        f.box(px + 28, py + 216, 180, 34)
        f.line(px + 110, py + 216, px + 110, py + 250, weight=1.25)
        f.text(px + 34, py + 236, "table 27G")
        f.text(px + 116, py + 236, "banks 72.7G")
        if i == 0:
            f.keyed(px + 14, py + 62, "A", px + 28, py + 62)
            f.keyed(px + 14, py + 115, "C", px + 28, py + 115)
            f.keyed(px + 14, py + 233, "D", px + 28, py + 233)
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
            (px + 36, py + 68),
        ]
    )
    f.lines(px + 34, py + 152, ["table rows:", "one coalesced", "read"])
    f.arrow(px + 138, py + 216, px + 138, py + 130)
    f.lines(
        px + 144,
        py + 152,
        ["expert rows:", "populated", "per layer,", "after", "routing"],
    )

    # 3 Forward
    px, py = slots[2]
    f.arrow(px + 144, py + 115, px + 156, py + 115)
    f.text(px + 34, py + 152, "lookup by compact local id")
    f.text(px + 34, py + 166, "hot experts on the GPU")
    f.text(px + 34, py + 194, "faults a chunk: 3.6M to 5k")
    return f


def main() -> None:
    for name, build in {
        "memory-tiers": memory_tiers,
        "decode-step": decode_step,
        "hot-set-swap": hot_set_swap,
        "prefill-chunk": prefill_chunk,
    }.items():
        (HERE / f"{name}.svg").write_text(build().svg(), encoding="utf-8")


if __name__ == "__main__":
    main()
