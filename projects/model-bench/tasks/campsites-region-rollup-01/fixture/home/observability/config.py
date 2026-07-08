from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SloConfig:
    target: float
    window_days: int
    query: str | None = None


@dataclass
class MetricConfig:
    key: str
    query: str | None = None
    static: str | None = None
    unit: str | None = None
    format: str | None = None


@dataclass
class SparkConfig:
    query: str


@dataclass
class NodeConfig:
    id: str
    label: str
    tier: str
    description: str
    group: str | None = None
    ingress: bool = False
    slo: SloConfig | None = None
    metrics: list[MetricConfig] = field(default_factory=list)
    spark: SparkConfig | None = None


@dataclass
class GroupConfig:
    id: str
    label: str
    tier: str
    description: str
    children: list[str]
    ingress: bool = False
    slo: SloConfig | None = None


@dataclass
class LinkerdEdge:
    """Pre-generated Linkerd metric queries for an HTTP edge."""

    rps_query: str
    latency_query: str
    error_rate_query: str


@dataclass
class EdgeConfig:
    source: str
    target: str
    bidi: bool = False
    linkerd: LinkerdEdge | None = None


@dataclass
class TopologyConfig:
    cache_ttl: int
    groups: list[GroupConfig]
    nodes: list[NodeConfig]
    edges: list[EdgeConfig]
