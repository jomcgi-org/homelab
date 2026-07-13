"""ClickHouse-backed topology builder (ADR 004 Layer 4 writer path).

Holds ``build_topology`` and its ClickHouse helpers, split out of
``home.observability.router`` so the public read endpoints (``/stats`` and
``/topology``, which only read precomputed Postgres snapshots) do not drag the
ClickHouse client, SLO math, and topology config into the public import closure.

Only the private monolith calls this: ``home.observability.rollup`` invokes
``build_topology`` on a schedule and upserts the payload into the
``observability.topology_snapshot`` table that the read endpoints serve from.
"""

from __future__ import annotations

import asyncio
import logging
import os

from home.observability.clickhouse import ClickHouseClient
from home.observability.config import (
    EdgeConfig,
    NodeConfig,
)
from home.observability.slo import (
    aggregate_group,
    compute_brief,
    compute_budget,
    compute_status,
)
from home.observability.topology_config import TOPOLOGY

logger = logging.getLogger(__name__)

_ch_semaphore = asyncio.Semaphore(2)
_CH_RETRIES = 1
_CH_RETRY_DELAY = 1.0


async def _ch_scalar(client: ClickHouseClient, query: str) -> float | None:
    """Execute a scalar query with one retry on transient failure."""
    for attempt in range(_CH_RETRIES + 1):
        try:
            async with _ch_semaphore:
                return await client.query_scalar(query)
        except Exception:
            if attempt < _CH_RETRIES:
                await asyncio.sleep(_CH_RETRY_DELAY)
            else:
                raise


async def _ch_rows(client: ClickHouseClient, query: str) -> list[dict]:
    """Execute a rows query with one retry on transient failure."""
    for attempt in range(_CH_RETRIES + 1):
        try:
            async with _ch_semaphore:
                return await client.query_rows(query)
        except Exception:
            if attempt < _CH_RETRIES:
                await asyncio.sleep(_CH_RETRY_DELAY)
            else:
                raise


async def _query_node(client: ClickHouseClient, node: NodeConfig) -> dict:
    """Execute all queries for a single node and return topology JSON.

    Acquires _ch_semaphore before each ClickHouse call to limit concurrent
    queries and avoid ClickHouse OOM-killing under memory pressure.
    """
    result: dict = {
        "id": node.id,
        "label": node.label,
        "tier": node.tier,
        "description": node.description,
    }
    if node.group:
        result["group"] = node.group
    if node.ingress:
        result["ingress"] = True

    # SLO query
    availability = None
    if node.slo and node.slo.query:
        try:
            availability = await _ch_scalar(client, node.slo.query)
        except Exception:
            logger.exception("SLO query failed for %s", node.id)

    if node.slo:
        result["slo"] = {"target": node.slo.target, "current": availability}
        if availability is not None:
            result["status"] = compute_status(availability, node.slo.target)
            result["budget"] = compute_budget(
                availability, node.slo.target, node.slo.window_days
            )
        else:
            result["status"] = "degraded"
    else:
        result["status"] = "healthy"

    # Metric queries
    metrics = []
    for m in node.metrics:
        if m.static is not None:
            metrics.append({"k": m.key, "v": m.static})
        elif m.query:
            try:
                val = await _ch_scalar(client, m.query)
                suffix = m.unit or ""
                v = f"{val}{suffix}" if val is not None else "—"
                metrics.append({"k": m.key, "v": v})
            except Exception:
                logger.exception("Metric query failed for %s.%s", node.id, m.key)
                metrics.append({"k": m.key, "v": "—"})
    result["metrics"] = metrics

    # Brief
    metrics_dict = {m["k"]: m["v"] for m in metrics}
    result["brief"] = compute_brief(availability, metrics_dict)

    # Spark query
    if node.spark:
        try:
            rows = await _ch_rows(client, node.spark.query)
            result["spark"] = [r.get("value", 0) for r in rows]
        except Exception:
            logger.exception("Spark query failed for %s", node.id)

    return result


async def _query_edge(client: ClickHouseClient, edge: EdgeConfig) -> dict:
    """Serialize an edge to its from/to (and bidi) shape.

    Inter-service edge metrics (RPS/latency/error rate) used to come from the
    Linkerd proxy telemetry; that mesh has been replaced by Cilium (ADR
    platform/012), so edges currently carry structure only. Cilium/Hubble edge
    metrics are a possible future re-addition here.
    """
    result: dict = {"from": edge.source, "to": edge.target}
    if edge.bidi:
        result["bidi"] = True
    return result


async def build_topology() -> dict:
    """Execute all queries and build the full topology response."""
    cfg = TOPOLOGY
    ch_url = os.environ.get("CLICKHOUSE_URL", "")
    client = ClickHouseClient(
        base_url=ch_url,
        user=os.environ.get("CLICKHOUSE_USER", ""),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )

    try:
        # Query all nodes and edges in parallel
        node_results, edge_results = await asyncio.gather(
            asyncio.gather(
                *[_query_node(client, n) for n in cfg.nodes],
                return_exceptions=True,
            ),
            asyncio.gather(
                *[_query_edge(client, e) for e in cfg.edges],
                return_exceptions=True,
            ),
        )

        # Build node lookup, handling exceptions
        nodes = []
        node_map: dict[str, dict] = {}
        for i, r in enumerate(node_results):
            if isinstance(r, Exception):
                logger.error("Node %s failed: %s", cfg.nodes[i].id, r)
                fallback = {
                    "id": cfg.nodes[i].id,
                    "label": cfg.nodes[i].label,
                    "tier": cfg.nodes[i].tier,
                    "description": cfg.nodes[i].description,
                    "status": "degraded",
                    "brief": "query error",
                    "metrics": [],
                }
                if cfg.nodes[i].group:
                    fallback["group"] = cfg.nodes[i].group
                nodes.append(fallback)
                node_map[cfg.nodes[i].id] = fallback
            else:
                nodes.append(r)
                node_map[r["id"]] = r

        # Aggregate groups
        groups = []
        for g in cfg.groups:
            children = [node_map[cid] for cid in g.children if cid in node_map]
            target = g.slo.target if g.slo else 99.0
            window = g.slo.window_days if g.slo else 30
            agg = aggregate_group(children, target, window)

            group_result = {
                "id": g.id,
                "label": g.label,
                "tier": g.tier,
                "description": g.description,
                "children": g.children,
                **agg,
            }
            if g.ingress:
                group_result["ingress"] = True
            groups.append(group_result)

        # Edges
        edges = []
        for i, r in enumerate(edge_results):
            if isinstance(r, Exception):
                e = cfg.edges[i]
                logger.error("Edge %s->%s failed: %s", e.source, e.target, r)
                edge: dict = {"from": e.source, "to": e.target}
                if e.bidi:
                    edge["bidi"] = True
                edges.append(edge)
            else:
                edges.append(r)

        return {"groups": groups, "nodes": nodes, "edges": edges}
    finally:
        await client.close()
