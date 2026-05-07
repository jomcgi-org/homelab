"""Unit tests for knowledge.layout — pure compute_layout function.

The layout runs FA2 on the connected subgraph, post-scales the result
to ``core_fraction`` of the canvas, and then applies a hard collide
post-process so no two node circles overlap. Orphan nodes (no edges)
are filtered upstream by ``KnowledgeStore.get_graph`` and never reach
this function — see ``store_test.py`` for that path.
"""

from __future__ import annotations

import math

import pytest

from knowledge.layout import EdgeRef, LayoutParams, NodePos, compute_layout


def _node(nid: str, x: float | None = None, y: float | None = None) -> NodePos:
    return NodePos(id=nid, prior_x=x, prior_y=y)


def _all_finite(positions: dict[str, tuple[float, float]]) -> bool:
    return all(math.isfinite(x) and math.isfinite(y) for x, y in positions.values())


# Render-radius constants — kept in sync with the production
# ``_RENDER_BASE_R`` / ``_RENDER_HUB_BOOST`` in ``layout.py`` so the
# overlap test asserts against the same circles the collide pass uses.
_BASE_R = 1.82 / 340
_HUB_BOOST = 0.325 / 340


def _expected_radius(degree: int) -> float:
    return _BASE_R + _HUB_BOOST * math.log2(1 + degree)


class TestComputeLayout:
    def test_compute_layout_is_deterministic_with_fixed_seed(self):
        """Same inputs + same seed produce byte-identical output dicts."""
        nodes = [_node("a"), _node("b"), _node("c"), _node("d")]
        edges = [EdgeRef("a", "b"), EdgeRef("b", "c"), EdgeRef("c", "d")]
        params = LayoutParams(seed=42)

        first = compute_layout(nodes, edges, params)
        second = compute_layout(nodes, edges, params)

        assert first == second

    # Note: the unit-level "fixed point under self-seeding" test was
    # removed because FA2 doesn't have a stable orientation on small
    # graphs — the cluster can rotate/reflect between passes even when
    # seeded with the previous output, so any small-graph drift threshold
    # is fragile. The integration test
    # ``test_reconcile_handler_preserves_positions_across_no_op_cycles``
    # in service_test.py exercises the same property end-to-end on a
    # realistic fixture, which is where the practical "no teleporting"
    # contract actually lives.

    def test_compute_layout_places_new_node_finitely(self):
        """A newcomer joining a prior-positioned graph gets a finite (x, y)."""
        nodes = [
            _node("a", 0.1, 0.2),
            _node("b", -0.3, 0.4),
            _node("new"),
        ]
        edges = [EdgeRef("a", "b"), EdgeRef("a", "new")]
        params = LayoutParams(seed=42)

        positions = compute_layout(nodes, edges, params)

        assert "new" in positions
        x, y = positions["new"]
        assert math.isfinite(x) and math.isfinite(y)

    def test_compute_layout_handles_empty_graph(self):
        assert compute_layout([], [], LayoutParams()) == {}

    def test_compute_layout_handles_single_orphan_returns_no_position(self):
        """A node with no edges is silently skipped — orphans are filtered upstream."""
        positions = compute_layout([_node("solo")], [], LayoutParams())
        assert positions == {}

    def test_compute_layout_handles_disconnected_components(self):
        """Two cliques with no shared nodes — all positioned, all in core."""
        nodes = [_node(n) for n in ("a", "b", "c", "x", "y", "z")]
        edges = [
            EdgeRef("a", "b"),
            EdgeRef("b", "c"),
            EdgeRef("a", "c"),
            EdgeRef("x", "y"),
            EdgeRef("y", "z"),
            EdgeRef("x", "z"),
        ]
        params = LayoutParams(seed=42)

        positions = compute_layout(nodes, edges, params)

        assert set(positions.keys()) == {n.id for n in nodes}
        assert _all_finite(positions)

    def test_compute_layout_filters_nan_inputs_via_module_contract(self):
        """A NaN prior must be ignored at the seed step, not propagated."""
        nodes = [
            _node("a", 0.1, 0.2),
            _node("b", float("nan"), float("nan")),
            _node("c", 0.3, -0.2),
        ]
        edges = [EdgeRef("a", "b"), EdgeRef("b", "c")]
        params = LayoutParams(seed=42)

        positions = compute_layout(nodes, edges, params)

        assert "b" in positions
        x, y = positions["b"]
        assert math.isfinite(x) and math.isfinite(y)

    def test_compute_layout_param_sensitivity(self):
        """Different gravity values produce different layouts."""
        nodes = [_node("a"), _node("b"), _node("c"), _node("d")]
        edges = [EdgeRef("a", "b"), EdgeRef("b", "c"), EdgeRef("c", "d")]

        loose = compute_layout(nodes, edges, LayoutParams(gravity=0.1, seed=42))
        tight = compute_layout(nodes, edges, LayoutParams(gravity=10.0, seed=42))

        assert loose != tight

    def test_compute_layout_skips_orphans_silently(self):
        """Mixed graph: connected nodes get positions, orphans get no key."""
        nodes = [
            _node("a"),
            _node("b"),
            _node("c"),
            _node("orph1"),
            _node("orph2"),
        ]
        edges = [EdgeRef("a", "b"), EdgeRef("b", "c"), EdgeRef("a", "c")]
        params = LayoutParams(seed=42)

        positions = compute_layout(nodes, edges, params)

        # Orphans get no entry in the output — they're expected to be
        # filtered upstream in ``KnowledgeStore.get_graph``; if any sneak
        # through, ``compute_layout`` silently skips them rather than
        # placing them on a perimeter ring.
        assert set(positions.keys()) == {"a", "b", "c"}
        assert _all_finite(positions)

    def test_compute_layout_handles_no_orphans_all_connected(self):
        """Every node is connected: only the FA2 + core-scale path runs."""
        nodes = [_node(nid) for nid in ("a", "b", "c")]
        edges = [EdgeRef("a", "b"), EdgeRef("b", "c"), EdgeRef("a", "c")]
        params = LayoutParams(seed=42)

        positions = compute_layout(nodes, edges, params)

        assert set(positions.keys()) == {"a", "b", "c"}
        assert _all_finite(positions)

    def test_compute_layout_with_node_size_scale_changes_output(self):
        """node_size_scale must be plumbed through to FA2 — same graph with
        scale=0 vs scale>0 yields different positions, proving the halo dict
        actually reaches the layout call.
        """
        nodes = [_node(nid) for nid in ("a", "b", "c", "d", "e")]
        edges = [
            EdgeRef("a", "b"),
            EdgeRef("a", "c"),
            EdgeRef("a", "d"),
            EdgeRef("a", "e"),
            EdgeRef("b", "c"),
        ]

        no_halo = compute_layout(
            nodes, edges, LayoutParams(node_size_scale=0.0, seed=42)
        )
        with_halo = compute_layout(
            nodes, edges, LayoutParams(node_size_scale=0.01, seed=42)
        )

        assert no_halo != with_halo

    def test_compute_layout_resolves_overlaps_in_dense_clusters(self):
        """The hard collide post-process must leave no two nodes overlapping.

        Seed a tight 3-node triangle (every node is hub-adjacent so FA2's
        own halo dict packs them as close as it can), call compute_layout,
        and assert that no two output positions have center distance less
        than ``rA + rB`` — i.e. circles never overlap. A small float
        epsilon accounts for the iterative pass not always fully
        converging on the last micro-overlap.
        """
        nodes = [_node(nid) for nid in ("a", "b", "c")]
        edges = [EdgeRef("a", "b"), EdgeRef("b", "c"), EdgeRef("a", "c")]
        params = LayoutParams(scaling_ratio=2.0, max_iter=100, seed=42)

        positions = compute_layout(nodes, edges, params)

        # Each node has degree 2 in this triangle; render radius is
        # the same for all three.
        r = _expected_radius(2)
        eps = 1e-6
        ids = list(positions)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                xa, ya = positions[ids[i]]
                xb, yb = positions[ids[j]]
                d = math.hypot(xa - xb, ya - yb)
                assert d >= 2 * r - eps, (
                    f"{ids[i]} and {ids[j]} overlap: distance={d}, min={2 * r}"
                )

    def test_compute_layout_handles_coincident_priors(self):
        """Two nodes seeded at exactly the same point are pushed apart.

        FA2 with identical prior positions can produce coincident output
        centers; the vectorized collide pass must invent a finite push
        direction (rather than dividing by zero) and separate them. This
        exercises the ``d2 == 0`` branch in :func:`_resolve_overlaps`.
        """
        nodes = [
            _node("a", 0.0, 0.0),
            _node("b", 0.0, 0.0),
            _node("c", 0.0, 0.0),
        ]
        edges = [EdgeRef("a", "b"), EdgeRef("b", "c"), EdgeRef("a", "c")]
        params = LayoutParams(seed=42)

        positions = compute_layout(nodes, edges, params)

        assert set(positions.keys()) == {"a", "b", "c"}
        assert _all_finite(positions)
        # The collide pass should have separated all pairs by at least the
        # sum of their render radii (degree=2 for every node here).
        r = _expected_radius(2)
        eps = 1e-6
        ids = list(positions)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                xa, ya = positions[ids[i]]
                xb, yb = positions[ids[j]]
                d = math.hypot(xa - xb, ya - yb)
                assert d >= 2 * r - eps, (
                    f"{ids[i]} and {ids[j]} overlap: distance={d}, min={2 * r}"
                )


class TestLayoutParamsValidation:
    def test_validates_positive_max_iter(self):
        with pytest.raises(ValueError, match="max_iter must be positive"):
            LayoutParams(max_iter=0)

    def test_validates_positive_scaling_ratio(self):
        with pytest.raises(ValueError, match="scaling_ratio must be positive"):
            LayoutParams(scaling_ratio=-1.0)

    def test_validates_finite_scaling_ratio(self):
        with pytest.raises(
            ValueError, match="scaling_ratio must be positive and finite"
        ):
            LayoutParams(scaling_ratio=float("inf"))

    def test_allows_zero_gravity(self):
        # Gravity is allowed to be zero (no center pull) — should not raise.
        params = LayoutParams(gravity=0.0)
        assert params.gravity == 0.0

    def test_validates_non_negative_gravity(self):
        with pytest.raises(ValueError, match="gravity must be non-negative and finite"):
            LayoutParams(gravity=-0.1)

    def test_validates_finite_gravity(self):
        with pytest.raises(ValueError, match="gravity must be non-negative and finite"):
            LayoutParams(gravity=float("nan"))

    def test_validates_core_fraction_upper_bound(self):
        with pytest.raises(ValueError, match=r"core_fraction must be in \(0, 1\]"):
            LayoutParams(core_fraction=1.5)

    def test_validates_core_fraction_lower_bound(self):
        with pytest.raises(ValueError, match=r"core_fraction must be in \(0, 1\]"):
            LayoutParams(core_fraction=0.0)

    def test_node_size_scale_zero_is_allowed(self):
        # node_size_scale=0 means "no halo" — must construct without raising.
        params = LayoutParams(node_size_scale=0.0)
        assert params.node_size_scale == 0.0

    def test_node_size_scale_validates_negative(self):
        with pytest.raises(ValueError, match="non-negative"):
            LayoutParams(node_size_scale=-0.001)

    def test_node_size_scale_validates_finite(self):
        with pytest.raises(ValueError, match="non-negative and finite"):
            LayoutParams(node_size_scale=float("inf"))


class TestLayoutParamsFromEnv:
    def test_uses_defaults_when_env_empty(self):
        params = LayoutParams.from_env({})
        assert params.scaling_ratio == 2.0
        assert params.gravity == 0.1
        assert params.max_iter == 100
        assert params.linlog is False
        assert params.core_fraction == 0.99
        assert params.node_size_scale == 0.005
        assert params.seed == 42

    def test_reads_overrides_from_env(self):
        params = LayoutParams.from_env(
            {
                "KNOWLEDGE_LAYOUT_SCALING_RATIO": "3.5",
                "KNOWLEDGE_LAYOUT_GRAVITY": "1.0",
                "KNOWLEDGE_LAYOUT_MAX_ITER": "200",
                "KNOWLEDGE_LAYOUT_LINLOG": "1",
                "KNOWLEDGE_LAYOUT_CORE_FRACTION": "0.8",
                "KNOWLEDGE_LAYOUT_NODE_SIZE_SCALE": "0.01",
                "KNOWLEDGE_LAYOUT_SEED": "7",
            }
        )
        assert params.scaling_ratio == 3.5
        assert params.gravity == 1.0
        assert params.max_iter == 200
        assert params.linlog is True
        assert params.core_fraction == 0.8
        assert params.node_size_scale == 0.01
        assert params.seed == 7

    @pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "True", "yes", "YES"])
    def test_parses_truthy_linlog_values(self, truthy: str):
        params = LayoutParams.from_env({"KNOWLEDGE_LAYOUT_LINLOG": truthy})
        assert params.linlog is True

    @pytest.mark.parametrize("falsy", ["0", "false", "FALSE", "no", "", "off", "nope"])
    def test_parses_falsy_linlog_values(self, falsy: str):
        params = LayoutParams.from_env({"KNOWLEDGE_LAYOUT_LINLOG": falsy})
        assert params.linlog is False

    def test_validates_invalid_env_values(self):
        with pytest.raises(ValueError):
            LayoutParams.from_env({"KNOWLEDGE_LAYOUT_MAX_ITER": "0"})
        with pytest.raises(ValueError):
            LayoutParams.from_env({"KNOWLEDGE_LAYOUT_SCALING_RATIO": "-0.1"})
        with pytest.raises(ValueError):
            LayoutParams.from_env({"KNOWLEDGE_LAYOUT_CORE_FRACTION": "2.0"})
        with pytest.raises(ValueError):
            LayoutParams.from_env({"KNOWLEDGE_LAYOUT_NODE_SIZE_SCALE": "-0.001"})
