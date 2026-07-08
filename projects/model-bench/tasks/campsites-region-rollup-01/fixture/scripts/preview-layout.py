#!/usr/bin/env python3
"""Standalone layout preview tool for the knowledge graph.

Loads a graph snapshot (JSON in the same shape as the /api/knowledge/graph
response), runs ``compute_layout`` with command-line-supplied params, and
writes a single self-contained HTML file you open in a browser to visualize
the layout. No force simulation runs in the browser — positions are baked
in by ``compute_layout`` exactly as they would be in prod.

The layout runs ``nx.forceatlas2_layout`` on the connected subgraph,
post-scales the result to ``--core-fraction``, and applies a hard
collide post-process so no two node circles overlap. This script's
``compute_layout`` call drops orphans and runs the hard collide pass
automatically — same path as prod, no orphan-ring step anymore (orphans
are filtered out at the API layer in production).

Usage:
    python preview-layout.py \\
        --snapshot graph.json \\
        --scaling-ratio 2.0 \\
        --gravity 0.1 \\
        --max-iter 100 \\
        --no-linlog \\
        --core-fraction 0.99 \\
        --node-size-scale 0.005 \\
        --seed 42 \\
        --out preview.html

Once you find params you like, copy them into
``projects/monolith/deploy/values.yaml`` and trigger
``homelab scheduler jobs run-now knowledge.layout`` to apply without
waiting for the next layout cycle.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

from knowledge.layout import EdgeRef, LayoutParams, NodePos, compute_layout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a knowledge-graph layout preview to a self-contained HTML file."
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        required=True,
        help="Path to a graph JSON snapshot ({nodes:[...], edges:[...]}).",
    )
    parser.add_argument("--scaling-ratio", type=float, default=2.0)
    parser.add_argument("--gravity", type=float, default=0.1)
    parser.add_argument("--max-iter", type=int, default=100)
    parser.add_argument(
        "--linlog",
        dest="linlog",
        action="store_true",
        default=False,
        help="Enable FA2 logarithmic attraction (default: off).",
    )
    parser.add_argument(
        "--no-linlog",
        dest="linlog",
        action="store_false",
        help="Disable FA2 logarithmic attraction.",
    )
    parser.add_argument("--core-fraction", type=float, default=0.99)
    parser.add_argument(
        "--node-size-scale",
        type=float,
        default=0.005,
        help="FA2 collision-avoidance halo per-node, scaled by node degree (0 disables).",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("preview.html"),
        help="Output HTML path (default: preview.html).",
    )
    args = parser.parse_args(argv)

    payload = json.loads(args.snapshot.read_text())
    nodes = [
        NodePos(
            id=n["id"],
            prior_x=n.get("x"),
            prior_y=n.get("y"),
        )
        for n in payload["nodes"]
    ]
    edges = [EdgeRef(source=e["source"], target=e["target"]) for e in payload["edges"]]
    params = LayoutParams(
        scaling_ratio=args.scaling_ratio,
        gravity=args.gravity,
        max_iter=args.max_iter,
        linlog=args.linlog,
        core_fraction=args.core_fraction,
        node_size_scale=args.node_size_scale,
        seed=args.seed,
    )
    positions = compute_layout(nodes, edges, params)
    args.out.write_text(_render_html(payload, positions, params))
    print(f"Wrote {args.out} ({len(positions)} positioned of {len(nodes)} nodes)")
    return 0


def _render_html(
    payload: dict,
    positions: dict[str, tuple[float, float]],
    params: LayoutParams,
) -> str:
    """Render a single self-contained HTML document with baked-in positions.

    Nodes without a computed position (filtered out by compute_layout for
    NaN/Inf) fall back to (0, 0) so the file is still well-formed.
    """
    nodes_with_pos = [
        {
            **n,
            "x": positions.get(n["id"], (0.0, 0.0))[0],
            "y": positions.get(n["id"], (0.0, 0.0))[1],
        }
        for n in payload["nodes"]
    ]
    data = json.dumps({"nodes": nodes_with_pos, "edges": payload["edges"]})
    title = html.escape(
        f"layout preview (sr={params.scaling_ratio}, g={params.gravity}, "
        f"it={params.max_iter}, ll={params.linlog}, "
        f"core={params.core_fraction}, "
        f"nss={params.node_size_scale}, seed={params.seed})"
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="margin:0;background:#111;color:#eee;font-family:sans-serif">
<h1 style="padding:1rem;margin:0;font-size:1rem;font-weight:normal">{title}</h1>
<svg width="100%" height="800" viewBox="-1.0 -1.0 2.0 2.0" preserveAspectRatio="xMidYMid meet" style="background:#111">
<g id="g"></g>
</svg>
<script>
const data = {data};
const g = document.getElementById('g');
const ns = "http://www.w3.org/2000/svg";
const byId = new Map(data.nodes.map(n => [n.id, n]));
for (const e of data.edges) {{
    const a = byId.get(e.source), b = byId.get(e.target);
    if (!a || !b) continue;
    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', a.x); line.setAttribute('y1', a.y);
    line.setAttribute('x2', b.x); line.setAttribute('y2', b.y);
    line.setAttribute('stroke', '#444'); line.setAttribute('stroke-width', '0.0015');
    g.appendChild(line);
}}
for (const n of data.nodes) {{
    const c = document.createElementNS(ns, 'circle');
    c.setAttribute('cx', n.x); c.setAttribute('cy', n.y);
    c.setAttribute('r', '0.005'); c.setAttribute('fill', '#4af');
    const t = document.createElementNS(ns, 'title');
    t.textContent = n.title || n.id;
    c.appendChild(t);
    g.appendChild(c);
}}
</script></body></html>"""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
