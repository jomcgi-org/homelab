"""Topology configuration defined in Python — no YAML file I/O needed.

Helper functions generate the repeated ClickHouse SQL patterns so each node
definition stays concise.
"""

from __future__ import annotations

from home.observability.config import (
    EdgeConfig,
    GroupConfig,
    MetricConfig,
    NodeConfig,
    SloConfig,
    TopologyConfig,
)

WINDOW_DAYS = 30
SLO_TARGET = 98.0


def _container_ready_query(
    namespace: str, container: str, pod_prefix: str | None = None
) -> str:
    """SLO query: percentage of minutes where k8s.container.ready >= 1."""
    pod_filter = ""
    if pod_prefix is not None:
        pod_filter = (
            f"\n      AND JSONExtractString(labels, 'k8s.pod.name')"
            f" LIKE '{pod_prefix}%'"
        )
    return f"""\
WITH per_minute AS (
  SELECT intDiv(s.unix_milli, 60000) AS mb, max(s.value) AS ready
  FROM signoz_metrics.distributed_samples_v4 s
  WHERE s.metric_name = 'k8s.container.ready'
    AND s.fingerprint IN (
    SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
    WHERE metric_name = 'k8s.container.ready'
      AND JSONExtractString(labels, 'k8s.namespace.name') = '{namespace}'
      AND JSONExtractString(labels, 'k8s.container.name') = '{container}'{pod_filter}
  ) AND s.unix_milli >= toUnixTimestamp(now() - INTERVAL {WINDOW_DAYS} DAY) * 1000
  GROUP BY mb
)
SELECT round(countIf(ready >= 1) / count() * 100, 4) AS value FROM per_minute"""


def _envoy_success_rate_query(cluster_pattern: str) -> str:
    """SLO query: envoy upstream success rate (only 5xx = bad)."""
    return f"""\
WITH
bad AS (
  SELECT intDiv(unix_milli, 60000) AS mb, sum(value) AS v
  FROM signoz_metrics.distributed_samples_v4
  WHERE metric_name = 'envoy_cluster_upstream_rq_xx'
    AND fingerprint IN (
    SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
    WHERE metric_name = 'envoy_cluster_upstream_rq_xx'
      AND JSONExtractString(labels, 'envoy_cluster_name') LIKE '%{cluster_pattern}%'
      AND JSONExtractString(labels, 'envoy_response_code_class') = '5'
  ) AND unix_milli >= toUnixTimestamp(now() - INTERVAL {WINDOW_DAYS} DAY) * 1000
  GROUP BY mb
),
total AS (
  SELECT intDiv(unix_milli, 60000) AS mb, sum(value) AS v
  FROM signoz_metrics.distributed_samples_v4
  WHERE metric_name = 'envoy_cluster_upstream_rq_xx'
    AND fingerprint IN (
    SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
    WHERE metric_name = 'envoy_cluster_upstream_rq_xx'
      AND JSONExtractString(labels, 'envoy_cluster_name') LIKE '%{cluster_pattern}%'
  ) AND unix_milli >= toUnixTimestamp(now() - INTERVAL {WINDOW_DAYS} DAY) * 1000
  GROUP BY mb
)
SELECT round((1 - sum(coalesce(b.v, 0)) / sum(t.v)) * 100, 4) AS value
FROM total t LEFT JOIN bad b ON t.mb = b.mb"""


def _cnpg_up_query() -> str:
    """SLO query: CNPG collector up."""
    return f"""\
WITH per_minute AS (
  SELECT intDiv(s.unix_milli, 60000) AS mb, max(s.value) AS up
  FROM signoz_metrics.distributed_samples_v4 s
  WHERE s.metric_name = 'cnpg_collector_up'
    AND s.fingerprint IN (
    SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
    WHERE metric_name = 'cnpg_collector_up'
  ) AND s.unix_milli >= toUnixTimestamp(now() - INTERVAL {WINDOW_DAYS} DAY) * 1000
  GROUP BY mb
)
SELECT round(countIf(up >= 1) / count() * 100, 4) AS value FROM per_minute"""


def _envoy_rps_query(cluster_pattern: str) -> str:
    """Metric query: average requests per second over the last 5 minutes."""
    return f"""\
WITH per_minute AS (
  SELECT intDiv(unix_milli, 60000) AS mb, sum(value) AS v
  FROM signoz_metrics.distributed_samples_v4
  WHERE metric_name = 'envoy_cluster_upstream_rq_xx'
    AND fingerprint IN (
    SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
    WHERE metric_name = 'envoy_cluster_upstream_rq_xx'
      AND JSONExtractString(labels, 'envoy_cluster_name') LIKE '%{cluster_pattern}%'
  ) AND unix_milli >= toUnixTimestamp(now() - INTERVAL 5 MINUTE) * 1000
  GROUP BY mb
)
SELECT round(avg(v) / 60, 1) AS value FROM per_minute"""


def _envoy_avg_latency_query(cluster_pattern: str) -> str:
    """Metric query: average upstream latency (ms) from histogram sum/count."""
    return f"""\
WITH
s AS (
  SELECT max(value) AS v FROM signoz_metrics.distributed_samples_v4
  WHERE metric_name = 'envoy_cluster_upstream_rq_time.sum'
    AND fingerprint IN (
    SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
    WHERE metric_name = 'envoy_cluster_upstream_rq_time.sum'
      AND attrs['envoy_cluster_name'] LIKE '%{cluster_pattern}%'
  ) AND unix_milli >= toUnixTimestamp(now() - INTERVAL 5 MINUTE) * 1000
),
c AS (
  SELECT max(value) AS v FROM signoz_metrics.distributed_samples_v4
  WHERE metric_name = 'envoy_cluster_upstream_rq_time.count'
    AND fingerprint IN (
    SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
    WHERE metric_name = 'envoy_cluster_upstream_rq_time.count'
      AND attrs['envoy_cluster_name'] LIKE '%{cluster_pattern}%'
  ) AND unix_milli >= toUnixTimestamp(now() - INTERVAL 5 MINUTE) * 1000
)
SELECT round(s.v / c.v, 1) AS value FROM s, c WHERE c.v > 0"""


def _cnpg_backends_query() -> str:
    """Metric query: current active backends."""
    return """\
SELECT round(max(value)) AS value
FROM signoz_metrics.distributed_samples_v4
WHERE metric_name = 'cnpg_backends_total'
  AND fingerprint IN (
  SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
  WHERE metric_name = 'cnpg_backends_total'
) AND unix_milli >= toUnixTimestamp(now() - INTERVAL 5 MINUTE) * 1000"""


def _cnpg_db_size_query() -> str:
    """Metric query: database size in MB for the monolith database."""
    return """\
SELECT round(max(value) / 1048576, 1) AS value
FROM signoz_metrics.distributed_samples_v4
WHERE metric_name = 'cnpg_pg_database_size_bytes'
  AND fingerprint IN (
  SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
  WHERE metric_name = 'cnpg_pg_database_size_bytes'
    AND JSONExtractString(labels, 'datname') = 'monolith'
) AND unix_milli >= toUnixTimestamp(now() - INTERVAL 5 MINUTE) * 1000"""


def _seaweedfs_disk_query() -> str:
    """Metric query: total disk usage in GB."""
    return """\
SELECT round(max(value) / 1073741824, 1) AS value
FROM signoz_metrics.distributed_samples_v4
WHERE metric_name = 'SeaweedFS_volumeServer_total_disk_size'
  AND fingerprint IN (
  SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
  WHERE metric_name = 'SeaweedFS_volumeServer_total_disk_size'
) AND unix_milli >= toUnixTimestamp(now() - INTERVAL 5 MINUTE) * 1000"""


def _argocd_apps_synced_query() -> str:
    """Metric query: number of ArgoCD applications.

    argocd_app_info is a label-cardinality gauge — value is always 1, one
    series per app. Count distinct fingerprints instead of max(value).
    """
    return """\
SELECT count(DISTINCT fingerprint) AS value
FROM signoz_metrics.distributed_samples_v4
WHERE metric_name = 'argocd_app_info'
  AND fingerprint IN (
  SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
  WHERE metric_name = 'argocd_app_info'
) AND unix_milli >= toUnixTimestamp(now() - INTERVAL 5 MINUTE) * 1000"""


def _container_memory_mb_query(namespace: str, deployment: str) -> str:
    """Metric query: container memory usage in MB (via resource_attrs Map)."""
    return f"""\
SELECT round(max(value) / 1048576) AS value
FROM signoz_metrics.distributed_samples_v4
WHERE metric_name = 'container.memory.usage'
  AND fingerprint IN (
  SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
  WHERE metric_name = 'container.memory.usage'
    AND resource_attrs['k8s.namespace.name'] = '{namespace}'
    AND resource_attrs['k8s.deployment.name'] = '{deployment}'
) AND unix_milli >= toUnixTimestamp(now() - INTERVAL 5 MINUTE) * 1000"""


def _llamacpp_requests_query(deployment: str) -> str:
    """Metric query: active inference requests for a llama-cpp deployment."""
    return f"""\
SELECT round(max(value)) AS value
FROM signoz_metrics.distributed_samples_v4
WHERE metric_name = 'llamacpp:requests_processing'
  AND fingerprint IN (
  SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
  WHERE metric_name = 'llamacpp:requests_processing'
    AND resource_attrs['k8s.deployment.name'] = '{deployment}'
) AND unix_milli >= toUnixTimestamp(now() - INTERVAL 5 MINUTE) * 1000"""


def _llamacpp_tokens_query(deployment: str) -> str:
    """Metric query: tokens generated in the last 5 minutes (counter delta)."""
    return f"""\
SELECT round(max(value) - min(value)) AS value
FROM signoz_metrics.distributed_samples_v4
WHERE metric_name = 'llamacpp:tokens_predicted_total'
  AND fingerprint IN (
  SELECT DISTINCT fingerprint FROM signoz_metrics.distributed_time_series_v4_6hrs
  WHERE metric_name = 'llamacpp:tokens_predicted_total'
    AND resource_attrs['k8s.deployment.name'] = '{deployment}'
) AND unix_milli >= toUnixTimestamp(now() - INTERVAL 5 MINUTE) * 1000"""


def _slo(query: str) -> SloConfig:
    return SloConfig(target=SLO_TARGET, window_days=WINDOW_DAYS, query=query)


TOPOLOGY = TopologyConfig(
    cache_ttl=900,
    groups=[
        GroupConfig(
            id="monolith",
            label="MONOLITH",
            tier="critical",
            ingress=True,
            description="fastapi + sveltekit",
            children=["home", "knowledge", "chat", "mcp"],
            slo=SloConfig(target=SLO_TARGET, window_days=WINDOW_DAYS),
        ),
        GroupConfig(
            id="cluster",
            label="CLUSTER",
            tier="infra",
            description="k3s infrastructure",
            children=[
                "argocd",
                "signoz",
                "envoy-gateway",
                "longhorn",
                "seaweedfs",
                "otel-operator",
            ],
        ),
    ],
    nodes=[
        # --- ingress / external ---
        NodeConfig(
            id="external",
            label="EXTERNAL",
            tier="ingress",
            description="webpage · claude · cli",
            metrics=[MetricConfig(key="clients", static="webpage, claude, cli")],
        ),
        NodeConfig(
            id="discord",
            label="DISCORD",
            tier="ingress",
            description="discord api",
            metrics=[MetricConfig(key="service", static="discord api")],
        ),
        # --- critical path ---
        NodeConfig(
            id="cloudflare",
            label="CLOUDFLARE TUNNEL",
            tier="critical",
            description="cloudflare tunnel",
            slo=_slo(_container_ready_query("envoy-gateway-system", "cloudflared")),
        ),
        NodeConfig(
            id="home",
            label="HOME",
            tier="critical",
            group="monolith",
            description="dashboard + notes + schedule",
            slo=_slo(_envoy_success_rate_query("monolith-public")),
            metrics=[
                MetricConfig(
                    key="rps",
                    query=_envoy_rps_query("monolith-public"),
                ),
                MetricConfig(
                    key="latency",
                    query=_envoy_avg_latency_query("monolith-public"),
                    unit="ms",
                ),
            ],
        ),
        NodeConfig(
            id="knowledge",
            label="KNOWLEDGE",
            tier="critical",
            group="monolith",
            description="search · ingest · gardener",
            slo=_slo(_envoy_success_rate_query("monolith-private")),
            metrics=[
                MetricConfig(
                    key="rps",
                    query=_envoy_rps_query("monolith-private"),
                ),
                MetricConfig(
                    key="latency",
                    query=_envoy_avg_latency_query("monolith-private"),
                    unit="ms",
                ),
            ],
        ),
        NodeConfig(
            id="chat",
            label="CHAT",
            tier="critical",
            group="monolith",
            description="discord backfill + summarization",
            slo=_slo(_envoy_success_rate_query("monolith-private")),
            metrics=[
                MetricConfig(
                    key="rps",
                    query=_envoy_rps_query("monolith-private"),
                ),
            ],
        ),
        NodeConfig(
            id="postgres",
            label="POSTGRES",
            tier="critical",
            description="cnpg + pgvector",
            slo=_slo(_cnpg_up_query()),
            metrics=[
                MetricConfig(
                    key="backends",
                    query=_cnpg_backends_query(),
                ),
                MetricConfig(
                    key="size",
                    query=_cnpg_db_size_query(),
                    unit=" MB",
                ),
            ],
        ),
        NodeConfig(
            id="llama-cpp",
            label="QWEN 3",
            tier="critical",
            description="qwen 3 inference",
            slo=_slo(_container_ready_query("inference", "llama-server", "inference-")),
            metrics=[
                MetricConfig(key="reqs", query=_llamacpp_requests_query("inference")),
                MetricConfig(key="tokens", query=_llamacpp_tokens_query("inference")),
                MetricConfig(
                    key="mem",
                    query=_container_memory_mb_query("inference", "inference"),
                    unit=" MB",
                ),
            ],
        ),
        NodeConfig(
            id="voyage-embedder",
            label="VOYAGE EMBEDDER",
            tier="critical",
            description="voyage-4 embedding",
            slo=_slo(
                _container_ready_query(
                    "inference", "llama-server", "inference-embeddings-"
                )
            ),
            metrics=[
                MetricConfig(
                    key="reqs", query=_llamacpp_requests_query("inference-embeddings")
                ),
                MetricConfig(
                    key="tokens", query=_llamacpp_tokens_query("inference-embeddings")
                ),
                MetricConfig(
                    key="mem",
                    query=_container_memory_mb_query(
                        "inference", "inference-embeddings"
                    ),
                    unit=" MB",
                ),
            ],
        ),
        NodeConfig(
            id="context-forge",
            label="CONTEXT FORGE",
            tier="critical",
            ingress=True,
            description="mcp gateway",
            slo=_slo(_container_ready_query("mcp", "mcp-context-forge")),
        ),
        # --- mcp (inside monolith) ---
        NodeConfig(
            id="mcp",
            label="MCP",
            tier="critical",
            group="monolith",
            description="model context protocol server",
            slo=_slo(_container_ready_query("monolith", "backend")),
        ),
        # --- cluster / infra ---
        NodeConfig(
            id="argocd",
            label="ARGOCD",
            tier="infra",
            group="cluster",
            description="gitops controller",
            slo=_slo(_container_ready_query("argocd", "application-controller")),
            metrics=[
                MetricConfig(
                    key="apps",
                    query=_argocd_apps_synced_query(),
                ),
            ],
        ),
        NodeConfig(
            id="signoz",
            label="SIGNOZ",
            tier="infra",
            group="cluster",
            description="observability platform",
            slo=_slo(_container_ready_query("signoz", "signoz")),
        ),
        NodeConfig(
            id="envoy-gateway",
            label="ENVOY GATEWAY",
            tier="infra",
            group="cluster",
            description="api gateway",
            slo=_slo(_container_ready_query("envoy-gateway-system", "envoy-gateway")),
        ),
        NodeConfig(
            id="longhorn",
            label="LONGHORN",
            tier="infra",
            group="cluster",
            description="distributed storage",
            slo=_slo(_container_ready_query("longhorn", "longhorn-manager")),
        ),
        NodeConfig(
            id="seaweedfs",
            label="SEAWEEDFS",
            tier="infra",
            group="cluster",
            description="object storage",
            slo=_slo(_container_ready_query("seaweedfs", "seaweedfs")),
            metrics=[
                MetricConfig(
                    key="disk",
                    query=_seaweedfs_disk_query(),
                    unit=" GB",
                ),
            ],
        ),
        NodeConfig(
            id="otel-operator",
            label="OTEL OPERATOR",
            tier="infra",
            group="cluster",
            description="opentelemetry operator",
            slo=_slo(_container_ready_query("opentelemetry-operator", "manager")),
        ),
    ],
    edges=[
        EdgeConfig(source="external", target="cloudflare"),
        EdgeConfig(source="cloudflare", target="monolith"),
        EdgeConfig(source="knowledge", target="postgres"),
        EdgeConfig(source="knowledge", target="voyage-embedder"),
        EdgeConfig(source="knowledge", target="llama-cpp"),
        EdgeConfig(source="chat", target="llama-cpp"),
        EdgeConfig(source="chat", target="discord"),
        EdgeConfig(source="chat", target="knowledge"),
        EdgeConfig(source="mcp", target="knowledge"),
        EdgeConfig(source="context-forge", target="mcp"),
        EdgeConfig(source="cloudflare", target="context-forge"),
    ],
)
