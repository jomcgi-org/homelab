"""Unit tests for home.observability.topology_query.

Covers:
  _ch_scalar  -- happy path, retry on transient failure, failure after retries
  _ch_rows    -- happy path, retry on transient failure
  _query_node -- node without SLO, with SLO, with static + dynamic metrics,
                 with spark, SLO query fails, metric query fails
  _query_edge -- simple edge, bidi edge
  build_topology -- end-to-end with mocked client and patched TOPOLOGY
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from home.observability.config import (
    EdgeConfig,
    GroupConfig,
    MetricConfig,
    NodeConfig,
    SloConfig,
    SparkConfig,
    TopologyConfig,
)
from home.observability.topology_query import (
    _ch_rows,
    _ch_scalar,
    _query_edge,
    _query_node,
    build_topology,
)


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def _simple_node(
    id_: str = "svc",
    *,
    tier: str = "2",
    group: str | None = None,
    ingress: bool = False,
    slo: SloConfig | None = None,
    metrics: list[MetricConfig] | None = None,
    spark: SparkConfig | None = None,
) -> NodeConfig:
    return NodeConfig(
        id=id_,
        label=id_.upper(),
        tier=tier,
        description=f"Desc {id_}",
        group=group,
        ingress=ingress,
        slo=slo,
        metrics=metrics or [],
        spark=spark,
    )


def _simple_edge(
    source: str = "a",
    target: str = "b",
    *,
    bidi: bool = False,
) -> EdgeConfig:
    return EdgeConfig(source=source, target=target, bidi=bidi)


def _mock_client(
    scalar_value: float | None = 99.5,
    rows_value: list[dict] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.query_scalar = AsyncMock(return_value=scalar_value)
    client.query_rows = AsyncMock(return_value=rows_value or [])
    client.close = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# _ch_scalar
# ---------------------------------------------------------------------------


class TestChScalar:
    @pytest.mark.asyncio
    async def test_returns_scalar_value(self):
        client = _mock_client(scalar_value=42.0)
        result = await _ch_scalar(client, "SELECT 42")
        assert result == 42.0

    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self):
        client = _mock_client(scalar_value=None)
        result = await _ch_scalar(client, "SELECT 1")
        assert result is None

    @pytest.mark.asyncio
    async def test_retries_once_on_transient_failure(self, monkeypatch):
        """First call raises, second returns a value -- retry must recover."""
        monkeypatch.setattr("home.observability.topology_query._CH_RETRY_DELAY", 0)
        call_count = 0

        async def _flaky(sql):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient")
            return 77.0

        client = MagicMock()
        client.query_scalar = _flaky
        result = await _ch_scalar(client, "SELECT 1")
        assert result == 77.0
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self, monkeypatch):
        monkeypatch.setattr("home.observability.topology_query._CH_RETRY_DELAY", 0)
        client = MagicMock()
        client.query_scalar = AsyncMock(side_effect=RuntimeError("DB down"))
        with pytest.raises(RuntimeError, match="DB down"):
            await _ch_scalar(client, "SELECT 1")


# ---------------------------------------------------------------------------
# _ch_rows
# ---------------------------------------------------------------------------


class TestChRows:
    @pytest.mark.asyncio
    async def test_returns_rows(self):
        rows = [{"bucket": 1, "value": 10.0}, {"bucket": 2, "value": 20.0}]
        client = _mock_client(rows_value=rows)
        result = await _ch_rows(client, "SELECT bucket, value")
        assert result == rows

    @pytest.mark.asyncio
    async def test_retries_once_on_transient_failure(self, monkeypatch):
        monkeypatch.setattr("home.observability.topology_query._CH_RETRY_DELAY", 0)
        call_count = 0

        async def _flaky(sql):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient")
            return [{"value": 5.0}]

        client = MagicMock()
        client.query_rows = _flaky
        result = await _ch_rows(client, "SELECT 1")
        assert result == [{"value": 5.0}]
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self, monkeypatch):
        monkeypatch.setattr("home.observability.topology_query._CH_RETRY_DELAY", 0)
        client = MagicMock()
        client.query_rows = AsyncMock(side_effect=RuntimeError("DB down"))
        with pytest.raises(RuntimeError, match="DB down"):
            await _ch_rows(client, "SELECT 1")


# ---------------------------------------------------------------------------
# _query_node
# ---------------------------------------------------------------------------


class TestQueryNode:
    @pytest.mark.asyncio
    async def test_node_without_slo_is_healthy(self):
        node = _simple_node("api")
        client = _mock_client()
        result = await _query_node(client, node)
        assert result["id"] == "api"
        assert result["status"] == "healthy"
        assert "slo" not in result

    @pytest.mark.asyncio
    async def test_node_basic_fields_populated(self):
        node = _simple_node("db")
        client = _mock_client()
        result = await _query_node(client, node)
        assert result["label"] == "DB"
        assert result["tier"] == "2"
        assert result["description"] == "Desc db"

    @pytest.mark.asyncio
    async def test_node_group_included_when_set(self):
        node = _simple_node("worker", group="compute")
        client = _mock_client()
        result = await _query_node(client, node)
        assert result["group"] == "compute"

    @pytest.mark.asyncio
    async def test_node_no_group_key_when_none(self):
        node = _simple_node("api", group=None)
        client = _mock_client()
        result = await _query_node(client, node)
        assert "group" not in result

    @pytest.mark.asyncio
    async def test_node_ingress_flag_included(self):
        node = _simple_node("edge", ingress=True)
        client = _mock_client()
        result = await _query_node(client, node)
        assert result["ingress"] is True

    @pytest.mark.asyncio
    async def test_node_with_slo_above_target_is_healthy(self):
        node = _simple_node(
            "api",
            slo=SloConfig(target=99.0, window_days=30, query="SELECT 1"),
        )
        client = _mock_client(scalar_value=99.9)
        result = await _query_node(client, node)
        assert result["status"] == "healthy"
        assert result["slo"]["target"] == 99.0
        assert result["slo"]["current"] == pytest.approx(99.9)

    @pytest.mark.asyncio
    async def test_node_with_slo_below_target_is_degraded(self):
        node = _simple_node(
            "api",
            slo=SloConfig(target=99.0, window_days=30, query="SELECT 1"),
        )
        client = _mock_client(scalar_value=95.0)
        result = await _query_node(client, node)
        assert result["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_node_slo_query_fails_results_in_degraded(self, monkeypatch):
        monkeypatch.setattr("home.observability.topology_query._CH_RETRY_DELAY", 0)
        node = _simple_node(
            "api",
            slo=SloConfig(target=99.0, window_days=30, query="SELECT 1"),
        )
        client = MagicMock()
        client.query_scalar = AsyncMock(side_effect=RuntimeError("CH down"))
        result = await _query_node(client, node)
        assert result["status"] == "degraded"
        assert result["slo"]["current"] is None

    @pytest.mark.asyncio
    async def test_node_static_metric_included(self):
        node = _simple_node(
            "cache",
            metrics=[MetricConfig(key="tier", static="L1")],
        )
        client = _mock_client()
        result = await _query_node(client, node)
        metrics = {m["k"]: m["v"] for m in result["metrics"]}
        assert metrics["tier"] == "L1"

    @pytest.mark.asyncio
    async def test_node_dynamic_metric_with_unit(self):
        node = _simple_node(
            "api",
            metrics=[MetricConfig(key="rps", query="SELECT rps", unit=" req/s")],
        )
        client = _mock_client(scalar_value=42.5)
        result = await _query_node(client, node)
        metrics = {m["k"]: m["v"] for m in result["metrics"]}
        assert metrics["rps"] == "42.5 req/s"

    @pytest.mark.asyncio
    async def test_node_dynamic_metric_query_fails_returns_dash(self, monkeypatch):
        monkeypatch.setattr("home.observability.topology_query._CH_RETRY_DELAY", 0)
        node = _simple_node(
            "api",
            metrics=[MetricConfig(key="rps", query="SELECT rps")],
        )
        client = MagicMock()
        client.query_scalar = AsyncMock(side_effect=RuntimeError("CH down"))
        result = await _query_node(client, node)
        metrics = {m["k"]: m["v"] for m in result["metrics"]}
        assert metrics["rps"] == "—"

    @pytest.mark.asyncio
    async def test_node_spark_populated_from_rows(self):
        node = _simple_node(
            "api",
            spark=SparkConfig(query="SELECT value"),
        )
        rows = [{"value": 1}, {"value": 2}, {"value": 3}]
        client = _mock_client(rows_value=rows)
        result = await _query_node(client, node)
        assert result["spark"] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_node_spark_absent_when_not_configured(self):
        node = _simple_node("api", spark=None)
        client = _mock_client()
        result = await _query_node(client, node)
        assert "spark" not in result

    @pytest.mark.asyncio
    async def test_node_brief_is_string(self):
        node = _simple_node("api")
        client = _mock_client()
        result = await _query_node(client, node)
        assert isinstance(result["brief"], str)


# ---------------------------------------------------------------------------
# _query_edge
# ---------------------------------------------------------------------------


class TestQueryEdge:
    @pytest.mark.asyncio
    async def test_simple_edge(self):
        edge = _simple_edge("a", "b")
        client = _mock_client()
        result = await _query_edge(client, edge)
        assert result == {"from": "a", "to": "b"}

    @pytest.mark.asyncio
    async def test_bidi_flag_included(self):
        edge = _simple_edge("a", "b", bidi=True)
        client = _mock_client()
        result = await _query_edge(client, edge)
        assert result.get("bidi") is True


# ---------------------------------------------------------------------------
# build_topology
# ---------------------------------------------------------------------------


def _make_topology(
    nodes: list[NodeConfig] | None = None,
    edges: list[EdgeConfig] | None = None,
    groups: list[GroupConfig] | None = None,
) -> TopologyConfig:
    return TopologyConfig(
        cache_ttl=60,
        nodes=nodes or [],
        edges=edges or [],
        groups=groups or [],
    )


class TestBuildTopology:
    @pytest.mark.asyncio
    async def test_returns_nodes_edges_groups_keys(self, monkeypatch):
        monkeypatch.setattr(
            "home.observability.topology_query.TOPOLOGY",
            _make_topology(),
        )
        mock_client = _mock_client()
        with patch(
            "home.observability.topology_query.ClickHouseClient",
            return_value=mock_client,
        ):
            result = await build_topology()
        assert "nodes" in result
        assert "edges" in result
        assert "groups" in result

    @pytest.mark.asyncio
    async def test_simple_node_appears_in_result(self, monkeypatch):
        node = _simple_node("api")
        monkeypatch.setattr(
            "home.observability.topology_query.TOPOLOGY",
            _make_topology(nodes=[node]),
        )
        mock_client = _mock_client()
        with patch(
            "home.observability.topology_query.ClickHouseClient",
            return_value=mock_client,
        ):
            result = await build_topology()
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["id"] == "api"

    @pytest.mark.asyncio
    async def test_node_exception_produces_fallback_degraded_node(self, monkeypatch):
        """A node that raises during query gets a degraded fallback entry."""
        node = _simple_node(
            "broken",
            slo=SloConfig(target=99.0, window_days=30, query="SELECT 1"),
        )
        monkeypatch.setattr(
            "home.observability.topology_query.TOPOLOGY",
            _make_topology(nodes=[node]),
        )
        monkeypatch.setattr("home.observability.topology_query._CH_RETRY_DELAY", 0)
        mock_client = MagicMock()
        mock_client.query_scalar = AsyncMock(side_effect=RuntimeError("CH exploded"))
        mock_client.close = AsyncMock()
        with patch(
            "home.observability.topology_query.ClickHouseClient",
            return_value=mock_client,
        ):
            result = await build_topology()
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["id"] == "broken"
        assert result["nodes"][0]["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_simple_edge_appears_in_result(self, monkeypatch):
        edge = _simple_edge("a", "b")
        monkeypatch.setattr(
            "home.observability.topology_query.TOPOLOGY",
            _make_topology(edges=[edge]),
        )
        mock_client = _mock_client()
        with patch(
            "home.observability.topology_query.ClickHouseClient",
            return_value=mock_client,
        ):
            result = await build_topology()
        assert len(result["edges"]) == 1
        assert result["edges"][0]["from"] == "a"
        assert result["edges"][0]["to"] == "b"

    @pytest.mark.asyncio
    async def test_group_aggregation_runs_without_error(self, monkeypatch):
        node = _simple_node("child1", slo=SloConfig(target=99.0, window_days=30))
        group = GroupConfig(
            id="g1",
            label="Group 1",
            tier="1",
            description="A group",
            children=["child1"],
            slo=SloConfig(target=99.0, window_days=30),
        )
        monkeypatch.setattr(
            "home.observability.topology_query.TOPOLOGY",
            _make_topology(nodes=[node], groups=[group]),
        )
        mock_client = _mock_client(scalar_value=99.9)
        with patch(
            "home.observability.topology_query.ClickHouseClient",
            return_value=mock_client,
        ):
            result = await build_topology()
        assert len(result["groups"]) == 1
        assert result["groups"][0]["id"] == "g1"

    @pytest.mark.asyncio
    async def test_client_closed_on_success(self, monkeypatch):
        monkeypatch.setattr(
            "home.observability.topology_query.TOPOLOGY",
            _make_topology(),
        )
        mock_client = _mock_client()
        with patch(
            "home.observability.topology_query.ClickHouseClient",
            return_value=mock_client,
        ):
            await build_topology()
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_client_closed_even_on_unexpected_error(self, monkeypatch):
        """The finally block closes the client even when a CH query fails.

        Give the node an SLO query so _ch_scalar is actually called and raises.
        The exception is caught inside _query_node's try/except, availability
        stays None, and the node comes back with status='degraded'. The key
        assertion is that close() is still called via the finally block.
        """
        node = _simple_node(
            "svc",
            slo=SloConfig(target=99.0, window_days=30, query="SELECT slo"),
        )
        monkeypatch.setattr(
            "home.observability.topology_query.TOPOLOGY",
            _make_topology(nodes=[node]),
        )
        monkeypatch.setattr("home.observability.topology_query._CH_RETRY_DELAY", 0)
        mock_client = MagicMock()
        # query_scalar always raises -- SLO query fails on every retry
        mock_client.query_scalar = AsyncMock(side_effect=RuntimeError("fatal"))
        mock_client.close = AsyncMock()
        with patch(
            "home.observability.topology_query.ClickHouseClient",
            return_value=mock_client,
        ):
            # Node errors are caught per-node (return_exceptions=True inside
            # _query_node), so build_topology returns successfully with a
            # degraded fallback node (availability=None -> status='degraded').
            result = await build_topology()
        assert result["nodes"][0]["status"] == "degraded"
        mock_client.close.assert_awaited_once()
