"""Server-side force-directed layout for the knowledge graph.

This module is intentionally pure: every public function takes inputs and
returns outputs with no I/O. The reconcile handler and the local preview
script both call ``compute_layout`` with identical ``LayoutParams`` so dev
and prod produce the same result.

Algorithm: FA2 on the connected graph, then a hard collide post-process.
====================================================================

The vault graph used to be bimodal (a connected core plus a long tail of
orphan notes), and we placed orphans on a perimeter ring at a hash-
determined angle. The orphan ring was dropped in feat/kg-layout-v2:
orphans now get filtered out at the API layer (see
``KnowledgeStore.get_graph``), so layout never sees them.

What remains:

* **Connected nodes** are laid out with ``nx.forceatlas2_layout``.
  FA2's ``node_size`` halo gives each node a degree-scaled collision-
  avoidance bubble (the d3-force ``collide`` analog), and we post-scale
  the FA2 result so its bounding box fills ``core_fraction`` of the
  unit canvas.
* **Hard collide post-process** runs after the post-scale, iteratively
  pushing apart any pair of node centers that sit closer than
  ``rA + rB + extra_pad`` — the same render-radius formula the Svelte
  component uses (``BASE_R + HUB_BOOST * log2(1+degree)`` normalised by
  the canvas span). Simultaneous-update Lloyd-style with a uniform
  spatial grid; ~120 iters, doesn't always fully converge in dense
  graphs but resolves the visible overlap.

This pipeline is layout-cycle stable: an unchanged graph produces an
unchanged layout (FA2 is seeded, the collide pass is deterministic).
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass

import networkx as nx

NoteId = str

# Render-radius formula constants. Must match the Svelte component
# (``KnowledgeGraph.svelte``) so the collide post-process resolves
# overlap in the same coordinate space the user sees. The 340 divisor
# is the approximate canvas span (the SVG viewBox is ~1.0 wide, the
# Svelte component renders into a ~340px tile in the typical layout).
_RENDER_BASE_R = 1.82 / 340
_RENDER_HUB_BOOST = 0.325 / 340

# Hard collide tuning. Hard-coded rather than exposed as Helm knobs to
# keep the surface small; revisit only if we see live tuning need.
_COLLIDE_MAX_ITER = 120
_COLLIDE_EXTRA_PAD = 0.003


@dataclass(frozen=True, slots=True)
class NodePos:
    id: NoteId
    prior_x: float | None
    prior_y: float | None


@dataclass(frozen=True, slots=True)
class EdgeRef:
    source: NoteId
    target: NoteId


def _is_truthy(value: str) -> bool:
    """Parse a string env value as a boolean.

    Truthy: ``"1"``, ``"true"``, ``"yes"`` (case-insensitive).
    Everything else (including empty string, ``"0"``, ``"false"``,
    ``"no"``) is falsy.
    """
    return value.strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True, slots=True)
class LayoutParams:
    scaling_ratio: float = 2.0
    gravity: float = 0.1
    max_iter: int = 100
    linlog: bool = False
    core_fraction: float = 0.99
    node_size_scale: float = 0.005
    seed: int = 42

    def __post_init__(self) -> None:
        if not (self.scaling_ratio > 0 and math.isfinite(self.scaling_ratio)):
            raise ValueError(
                f"scaling_ratio must be positive and finite, got {self.scaling_ratio}"
            )
        # Gravity may legitimately be 0 (no center pull). Disallow negatives
        # and non-finite values.
        if not (self.gravity >= 0 and math.isfinite(self.gravity)):
            raise ValueError(
                f"gravity must be non-negative and finite, got {self.gravity}"
            )
        if self.max_iter <= 0:
            raise ValueError(f"max_iter must be positive, got {self.max_iter}")
        if not (0 < self.core_fraction <= 1):
            raise ValueError(
                f"core_fraction must be in (0, 1], got {self.core_fraction}"
            )
        # node_size_scale may legitimately be 0 (no halo / no collision-avoidance).
        # Disallow negatives and non-finite values.
        if not (self.node_size_scale >= 0 and math.isfinite(self.node_size_scale)):
            raise ValueError(
                f"node_size_scale must be non-negative and finite, got {self.node_size_scale}"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LayoutParams":
        """Read layout knobs from environment variables, falling back to defaults.

        Invalid values raise ValueError via __post_init__ — the pod fails to
        start, ArgoCD surfaces CrashLoopBackOff, no silent fallback.
        """
        env = environ if environ is not None else os.environ
        return cls(
            scaling_ratio=float(env.get("KNOWLEDGE_LAYOUT_SCALING_RATIO", "2.0")),
            gravity=float(env.get("KNOWLEDGE_LAYOUT_GRAVITY", "0.1")),
            max_iter=int(env.get("KNOWLEDGE_LAYOUT_MAX_ITER", "100")),
            linlog=_is_truthy(env.get("KNOWLEDGE_LAYOUT_LINLOG", "false")),
            core_fraction=float(env.get("KNOWLEDGE_LAYOUT_CORE_FRACTION", "0.99")),
            node_size_scale=float(env.get("KNOWLEDGE_LAYOUT_NODE_SIZE_SCALE", "0.005")),
            seed=int(env.get("KNOWLEDGE_LAYOUT_SEED", "42")),
        )


def _resolve_overlaps(
    positions: dict[NoteId, tuple[float, float]],
    degrees: dict[NoteId, int],
    *,
    max_iter: int,
    extra_pad: float,
) -> dict[NoteId, tuple[float, float]]:
    """Iterative hard collision resolution.

    Enforce that no two node centers are closer than
    ``(rA + rB + extra_pad)``, where ``rA`` / ``rB`` derive from the
    same render-radius formula the Svelte component uses
    (``BASE_R + HUB_BOOST * log2(1+degree)``, normalized by canvas
    span ~340).

    Uses simultaneous update (accumulate displacements per node, apply
    once per iteration) and uniform-grid spatial indexing for O(N) per
    iter. Doesn't always fully converge in dense graphs — best effort.
    Returns a new dict; doesn't mutate the input.
    """
    radii = {
        nid: _RENDER_BASE_R + _RENDER_HUB_BOOST * math.log2(1 + degrees.get(nid, 0))
        for nid in positions
    }
    if not radii:
        return dict(positions)
    max_r = max(radii.values())
    cell_size = max(2 * max_r + extra_pad, 1e-6)

    # Mutable working copy.
    pos = {nid: list(xy) for nid, xy in positions.items()}
    ids = list(pos)

    for _ in range(max_iter):
        grid: dict[tuple[int, int], list[NoteId]] = {}
        for nid in ids:
            x, y = pos[nid]
            grid.setdefault((int(x / cell_size), int(y / cell_size)), []).append(nid)

        deltas = {nid: [0.0, 0.0] for nid in ids}
        moved = 0
        for nid_a in ids:
            xa, ya = pos[nid_a]
            ra = radii[nid_a]
            cax, cay = int(xa / cell_size), int(ya / cell_size)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for nid_b in grid.get((cax + dx, cay + dy), ()):
                        if nid_b <= nid_a:
                            continue
                        xb, yb = pos[nid_b]
                        rb = radii[nid_b]
                        min_dist = ra + rb + extra_pad
                        ddx, ddy = xa - xb, ya - yb
                        d2 = ddx * ddx + ddy * ddy
                        if d2 == 0:
                            ddx, ddy, d = 1e-6, 0.0, 1e-6
                        else:
                            d = math.sqrt(d2)
                        if d < min_dist:
                            push = (min_dist - d) / 2
                            ux, uy = ddx / d, ddy / d
                            deltas[nid_a][0] += ux * push
                            deltas[nid_a][1] += uy * push
                            deltas[nid_b][0] -= ux * push
                            deltas[nid_b][1] -= uy * push
                            moved += 1

        if moved == 0:
            break
        for nid in ids:
            d_ = deltas[nid]
            if d_[0] != 0.0 or d_[1] != 0.0:
                pos[nid][0] += d_[0]
                pos[nid][1] += d_[1]

    return {nid: (xy[0], xy[1]) for nid, xy in pos.items()}


def compute_layout(
    nodes: list[NodePos],
    edges: list[EdgeRef],
    params: LayoutParams,
) -> dict[NoteId, tuple[float, float]]:
    """Compute (x, y) positions via FA2 + hard-collide post-process.

    See the module docstring for the algorithm. In short:

    1. Build the connected subgraph (nodes touched by an edge, plus
       all the edges between them) and run ``nx.forceatlas2_layout``
       seeded with each node's ``prior_x``/``prior_y`` if both are
       finite.
    2. Post-scale so the bounding box fills ``core_fraction`` of the
       unit canvas.
    3. Run :func:`_resolve_overlaps` to push apart any remaining
       overlapping node circles.

    Orphan nodes (no edges) are expected to be filtered out upstream
    in :meth:`KnowledgeStore.get_graph`; if any sneak through they are
    silently skipped (no ring placement).

    Non-finite outputs (NaN/Inf) are filtered out. Caller treats
    missing positions as "use random-center fallback at render time."
    """
    if not nodes:
        return {}

    edge_endpoints: set[NoteId] = set()
    for e in edges:
        edge_endpoints.add(e.source)
        edge_endpoints.add(e.target)

    connected = [n for n in nodes if n.id in edge_endpoints]

    out: dict[NoteId, tuple[float, float]] = {}

    if not connected:
        return out

    g = nx.Graph()
    connected_ids = {n.id for n in connected}
    for n in connected:
        g.add_node(n.id)
    for e in edges:
        if e.source in connected_ids and e.target in connected_ids:
            g.add_edge(e.source, e.target)

    # NetworkX forceatlas2_layout requires pos values to be array-like
    # (it calls .copy() on each), not tuples — use lists.
    prior: dict[NoteId, list[float]] = {
        n.id: [n.prior_x, n.prior_y]
        for n in connected
        if n.prior_x is not None
        and n.prior_y is not None
        and math.isfinite(n.prior_x)
        and math.isfinite(n.prior_y)
    }

    # Compute degrees once: used both for FA2's halo dict and the
    # post-process collide pass.
    degrees: dict[NoteId, int] = {n.id: 0 for n in connected}
    for e in edges:
        if e.source in degrees:
            degrees[e.source] += 1
        if e.target in degrees:
            degrees[e.target] += 1

    kwargs: dict = {
        "pos": prior or None,
        "max_iter": params.max_iter,
        "scaling_ratio": params.scaling_ratio,
        "gravity": params.gravity,
        "linlog": params.linlog,
        "seed": params.seed,
    }
    # When node_size_scale > 0, pass a per-node halo radius dict to FA2
    # for collision-avoidance (analogous to d3-force's `collide`). The
    # halo grows logarithmically with degree, so high-degree hubs claim
    # a slightly larger personal-space bubble than leaf nodes. Skip the
    # dict construction entirely when scale=0 — the param is opt-out.
    if params.node_size_scale > 0:
        kwargs["node_size"] = {
            nid: params.node_size_scale * (1 + math.log2(1 + degrees.get(nid, 0)))
            for nid in connected_ids
        }
    raw = nx.forceatlas2_layout(g, **kwargs)

    # Find FA2's bounding box across finite outputs and post-scale to
    # fill `core_fraction` of the unit canvas. Skip the rescale entirely
    # when every coordinate is zero (single connected node, etc.) to
    # avoid a divide-by-zero.
    finite = {
        nid: (float(x), float(y))
        for nid, (x, y) in raw.items()
        if math.isfinite(float(x)) and math.isfinite(float(y))
    }
    if finite:
        max_extent = max(
            (max(abs(x), abs(y)) for x, y in finite.values()),
            default=0.0,
        )
        if max_extent > 0:
            scale_factor = params.core_fraction / max_extent
        else:
            scale_factor = 1.0
        for nid, (x, y) in finite.items():
            out[nid] = (x * scale_factor, y * scale_factor)

    # Hard collide post-process: simultaneous-update Lloyd-style
    # relaxation that pushes apart any node-center pair closer than
    # the sum of their render radii. Doesn't always fully converge in
    # dense graphs but visibly clears the residual stacking that FA2's
    # halo dict alone leaves behind.
    out = _resolve_overlaps(
        out,
        degrees,
        max_iter=_COLLIDE_MAX_ITER,
        extra_pad=_COLLIDE_EXTRA_PAD,
    )

    return out
