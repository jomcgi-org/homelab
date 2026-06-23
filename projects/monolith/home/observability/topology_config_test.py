"""Tests for topology_config.py query-builder functions.

The TOPOLOGY *object* shape (node/edge/group counts and cross-reference
integrity) is already covered by observability/config_test.py.  These
tests focus on the helper functions that generate ClickHouse SQL strings
and on the _slo() / _linkerd() compositors.
"""

from __future__ import annotations

import pytest

from home.observability.config import LinkerdEdge, SloConfig
from home.observability.topology_config import (
    WINDOW_DAYS,
    SLO_TARGET,
    _argocd_apps_synced_query,
    _cnpg_backends_query,
    _cnpg_db_size_query,
    _cnpg_up_query,
    _container_memory_mb_query,
    _container_ready_query,
    _envoy_avg_latency_query,
    _envoy_rps_query,
    _envoy_success_rate_query,
    _linkerd,
    _linkerd_p99_error_rate_query,
    _linkerd_p99_latency_query,
    _linkerd_p99_rps_query,
    _llamacpp_requests_query,
    _llamacpp_tokens_query,
    _seaweedfs_disk_query,
    _slo,
)


class TestModuleConstants:
    def test_window_days(self):
        assert WINDOW_DAYS == 30

    def test_slo_target(self):
        assert SLO_TARGET == 98.0


class TestSloCompositor:
    def test_returns_slo_config(self):
        result = _slo("SELECT 1")
        assert isinstance(result, SloConfig)

    def test_target_equals_module_constant(self):
        result = _slo("SELECT 1")
        assert result.target == SLO_TARGET

    def test_window_days_equals_module_constant(self):
        result = _slo("SELECT 1")
        assert result.window_days == WINDOW_DAYS

    def test_query_is_preserved(self):
        q = "SELECT round(1) AS value"
        result = _slo(q)
        assert result.query == q


class TestContainerReadyQuery:
    def test_returns_string(self):
        q = _container_ready_query("my-ns", "my-container")
        assert isinstance(q, str)
        assert len(q) > 0

    def test_contains_namespace(self):
        q = _container_ready_query("my-ns", "my-container")
        assert "my-ns" in q

    def test_contains_container(self):
        q = _container_ready_query("my-ns", "my-container")
        assert "my-container" in q

    def test_contains_metric_name(self):
        q = _container_ready_query("my-ns", "my-container")
        assert "k8s.container.ready" in q

    def test_contains_window_days(self):
        q = _container_ready_query("my-ns", "my-container")
        assert f"INTERVAL {WINDOW_DAYS} DAY" in q

    def test_no_pod_prefix_by_default(self):
        q = _container_ready_query("my-ns", "my-container")
        assert "k8s.pod.name" not in q

    def test_pod_prefix_included_when_provided(self):
        q = _container_ready_query("my-ns", "my-container", pod_prefix="my-pod-")
        assert "k8s.pod.name" in q
        assert "my-pod-%" in q

    def test_pod_prefix_none_same_as_omitted(self):
        q_none = _container_ready_query("my-ns", "my-container", pod_prefix=None)
        q_omit = _container_ready_query("my-ns", "my-container")
        assert q_none == q_omit

    def test_select_clause(self):
        q = _container_ready_query("my-ns", "my-container")
        assert "SELECT" in q
        assert "value" in q


class TestEnvoySuccessRateQuery:
    def test_returns_string(self):
        q = _envoy_success_rate_query("my-cluster")
        assert isinstance(q, str)

    def test_contains_cluster_pattern(self):
        q = _envoy_success_rate_query("my-cluster")
        assert "my-cluster" in q

    def test_contains_5xx_filter(self):
        q = _envoy_success_rate_query("my-cluster")
        assert "'5'" in q

    def test_contains_window_days(self):
        q = _envoy_success_rate_query("my-cluster")
        assert f"INTERVAL {WINDOW_DAYS} DAY" in q

    def test_contains_metric_name(self):
        q = _envoy_success_rate_query("my-cluster")
        assert "envoy_cluster_upstream_rq_xx" in q

    def test_cluster_pattern_used_in_like(self):
        q = _envoy_success_rate_query("monolith-public")
        assert "%monolith-public%" in q


class TestCnpgUpQuery:
    def test_returns_string(self):
        q = _cnpg_up_query()
        assert isinstance(q, str)

    def test_contains_metric_name(self):
        q = _cnpg_up_query()
        assert "cnpg_collector_up" in q

    def test_contains_window_days(self):
        q = _cnpg_up_query()
        assert f"INTERVAL {WINDOW_DAYS} DAY" in q


class TestEnvoyRpsQuery:
    def test_returns_string(self):
        q = _envoy_rps_query("my-cluster")
        assert isinstance(q, str)

    def test_contains_cluster_pattern(self):
        q = _envoy_rps_query("my-cluster")
        assert "my-cluster" in q

    def test_uses_5_minute_window(self):
        q = _envoy_rps_query("my-cluster")
        assert "INTERVAL 5 MINUTE" in q

    def test_contains_metric_name(self):
        q = _envoy_rps_query("my-cluster")
        assert "envoy_cluster_upstream_rq_xx" in q


class TestEnvoyAvgLatencyQuery:
    def test_returns_string(self):
        q = _envoy_avg_latency_query("my-cluster")
        assert isinstance(q, str)

    def test_contains_cluster_pattern(self):
        q = _envoy_avg_latency_query("my-cluster")
        assert "my-cluster" in q

    def test_uses_5_minute_window(self):
        q = _envoy_avg_latency_query("my-cluster")
        assert "INTERVAL 5 MINUTE" in q

    def test_contains_latency_metric(self):
        q = _envoy_avg_latency_query("my-cluster")
        assert "envoy_cluster_upstream_rq_time" in q


class TestCnpgBackendsQuery:
    def test_returns_string(self):
        q = _cnpg_backends_query()
        assert isinstance(q, str)

    def test_contains_metric_name(self):
        q = _cnpg_backends_query()
        assert "cnpg_backends_total" in q

    def test_uses_5_minute_window(self):
        q = _cnpg_backends_query()
        assert "INTERVAL 5 MINUTE" in q


class TestCnpgDbSizeQuery:
    def test_returns_string(self):
        q = _cnpg_db_size_query()
        assert isinstance(q, str)

    def test_contains_metric_name(self):
        q = _cnpg_db_size_query()
        assert "cnpg_pg_database_size_bytes" in q

    def test_filters_monolith_database(self):
        q = _cnpg_db_size_query()
        assert "'monolith'" in q

    def test_converts_to_mb(self):
        # divides by 1048576 (bytes → MB)
        q = _cnpg_db_size_query()
        assert "1048576" in q


class TestSeaweedFsDiskQuery:
    def test_returns_string(self):
        q = _seaweedfs_disk_query()
        assert isinstance(q, str)

    def test_contains_metric_name(self):
        q = _seaweedfs_disk_query()
        assert "SeaweedFS_volumeServer_total_disk_size" in q

    def test_converts_to_gb(self):
        # divides by 1073741824 (bytes → GB)
        q = _seaweedfs_disk_query()
        assert "1073741824" in q


class TestArgoCdAppsSyncedQuery:
    def test_returns_string(self):
        q = _argocd_apps_synced_query()
        assert isinstance(q, str)

    def test_contains_metric_name(self):
        q = _argocd_apps_synced_query()
        assert "argocd_app_info" in q

    def test_counts_distinct_fingerprints(self):
        # Uses count(DISTINCT fingerprint) not max(value)
        q = _argocd_apps_synced_query()
        assert "DISTINCT fingerprint" in q


class TestContainerMemoryMbQuery:
    def test_returns_string(self):
        q = _container_memory_mb_query("my-ns", "my-deployment")
        assert isinstance(q, str)

    def test_contains_namespace(self):
        q = _container_memory_mb_query("my-ns", "my-deployment")
        assert "my-ns" in q

    def test_contains_deployment(self):
        q = _container_memory_mb_query("my-ns", "my-deployment")
        assert "my-deployment" in q

    def test_contains_metric_name(self):
        q = _container_memory_mb_query("my-ns", "my-deployment")
        assert "container.memory.usage" in q

    def test_converts_to_mb(self):
        q = _container_memory_mb_query("my-ns", "my-deployment")
        assert "1048576" in q


class TestLlamaCppRequestsQuery:
    def test_returns_string(self):
        q = _llamacpp_requests_query("llama-cpp")
        assert isinstance(q, str)

    def test_contains_deployment(self):
        q = _llamacpp_requests_query("llama-cpp")
        assert "llama-cpp" in q

    def test_contains_metric_name(self):
        q = _llamacpp_requests_query("llama-cpp")
        assert "llamacpp:requests_processing" in q

    def test_different_deployment_names(self):
        q1 = _llamacpp_requests_query("llama-cpp")
        q2 = _llamacpp_requests_query("llama-cpp-embeddings")
        assert "llama-cpp-embeddings" in q2
        assert q1 != q2


class TestLlamaCppTokensQuery:
    def test_returns_string(self):
        q = _llamacpp_tokens_query("llama-cpp")
        assert isinstance(q, str)

    def test_contains_deployment(self):
        q = _llamacpp_tokens_query("llama-cpp")
        assert "llama-cpp" in q

    def test_contains_metric_name(self):
        q = _llamacpp_tokens_query("llama-cpp")
        assert "llamacpp:tokens_predicted_total" in q

    def test_computes_counter_delta(self):
        # max - min gives counter delta
        q = _llamacpp_tokens_query("llama-cpp")
        assert "max(value) - min(value)" in q


class TestLinkerdP99RpsQuery:
    def test_returns_string(self):
        q = _linkerd_p99_rps_query("monolith", "llama-cpp", "llama-cpp")
        assert isinstance(q, str)

    def test_contains_src(self):
        q = _linkerd_p99_rps_query("monolith", "llama-cpp", "llama-cpp")
        assert "monolith" in q

    def test_contains_dst_ns(self):
        q = _linkerd_p99_rps_query("src-svc", "dst-namespace", "dst-svc")
        assert "dst-namespace" in q

    def test_contains_dst_svc(self):
        q = _linkerd_p99_rps_query("src-svc", "dst-ns", "dst-svc")
        assert "dst-svc" in q

    def test_contains_metric_name(self):
        q = _linkerd_p99_rps_query("a", "b", "c")
        assert "outbound_http_route_request_duration_seconds.count" in q

    def test_uses_7_day_window(self):
        q = _linkerd_p99_rps_query("a", "b", "c")
        assert "INTERVAL 7 DAY" in q

    def test_uses_p99_quantile(self):
        q = _linkerd_p99_rps_query("a", "b", "c")
        assert "0.99" in q


class TestLinkerdP99LatencyQuery:
    def test_returns_string(self):
        q = _linkerd_p99_latency_query("monolith", "llama-cpp", "llama-cpp")
        assert isinstance(q, str)

    def test_contains_src(self):
        q = _linkerd_p99_latency_query("monolith", "llama-cpp", "llama-cpp")
        assert "monolith" in q

    def test_contains_dst_ns(self):
        q = _linkerd_p99_latency_query("src-svc", "dst-namespace", "dst-svc")
        assert "dst-namespace" in q

    def test_contains_dst_svc(self):
        q = _linkerd_p99_latency_query("src-svc", "dst-ns", "dst-svc")
        assert "dst-svc" in q

    def test_contains_bucket_metric(self):
        q = _linkerd_p99_latency_query("a", "b", "c")
        assert "outbound_http_route_request_duration_seconds.bucket" in q

    def test_converts_to_ms(self):
        # multiplies by 1000 for ms conversion
        q = _linkerd_p99_latency_query("a", "b", "c")
        assert "1000" in q

    def test_uses_7_day_window(self):
        q = _linkerd_p99_latency_query("a", "b", "c")
        assert "INTERVAL 7 DAY" in q


class TestLinkerdP99ErrorRateQuery:
    def test_returns_string(self):
        q = _linkerd_p99_error_rate_query("monolith", "llama-cpp", "llama-cpp")
        assert isinstance(q, str)

    def test_contains_src(self):
        q = _linkerd_p99_error_rate_query("monolith", "llama-cpp", "llama-cpp")
        assert "monolith" in q

    def test_contains_dst_ns(self):
        q = _linkerd_p99_error_rate_query("src-svc", "dst-namespace", "dst-svc")
        assert "dst-namespace" in q

    def test_contains_dst_svc(self):
        q = _linkerd_p99_error_rate_query("src-svc", "dst-ns", "dst-svc")
        assert "dst-svc" in q

    def test_contains_metric_name(self):
        q = _linkerd_p99_error_rate_query("a", "b", "c")
        assert "outbound_http_route_backend_response_statuses_total" in q

    def test_filters_5xx_errors(self):
        q = _linkerd_p99_error_rate_query("a", "b", "c")
        assert "5%" in q

    def test_uses_7_day_window(self):
        q = _linkerd_p99_error_rate_query("a", "b", "c")
        assert "INTERVAL 7 DAY" in q

    def test_uses_p99_quantile(self):
        q = _linkerd_p99_error_rate_query("a", "b", "c")
        assert "0.99" in q


class TestLinkerdCompositor:
    def test_returns_linkerd_edge(self):
        result = _linkerd("monolith", "llama-cpp", "llama-cpp")
        assert isinstance(result, LinkerdEdge)

    def test_rps_query_populated(self):
        result = _linkerd("monolith", "llama-cpp", "llama-cpp")
        assert isinstance(result.rps_query, str)
        assert len(result.rps_query) > 0

    def test_latency_query_populated(self):
        result = _linkerd("monolith", "llama-cpp", "llama-cpp")
        assert isinstance(result.latency_query, str)
        assert len(result.latency_query) > 0

    def test_error_rate_query_populated(self):
        result = _linkerd("monolith", "llama-cpp", "llama-cpp")
        assert isinstance(result.error_rate_query, str)
        assert len(result.error_rate_query) > 0

    def test_all_three_queries_distinct(self):
        result = _linkerd("monolith", "llama-cpp", "llama-cpp")
        assert result.rps_query != result.latency_query
        assert result.latency_query != result.error_rate_query
        assert result.rps_query != result.error_rate_query

    def test_src_propagated_to_all_queries(self):
        result = _linkerd("unique-src", "dst-ns", "dst-svc")
        assert "unique-src" in result.rps_query
        assert "unique-src" in result.latency_query
        assert "unique-src" in result.error_rate_query

    def test_dst_ns_propagated_to_all_queries(self):
        result = _linkerd("src", "unique-dst-ns", "dst-svc")
        assert "unique-dst-ns" in result.rps_query
        assert "unique-dst-ns" in result.latency_query
        assert "unique-dst-ns" in result.error_rate_query

    def test_dst_svc_propagated_to_all_queries(self):
        result = _linkerd("src", "dst-ns", "unique-dst-svc")
        assert "unique-dst-svc" in result.rps_query
        assert "unique-dst-svc" in result.latency_query
        assert "unique-dst-svc" in result.error_rate_query


class TestQueryParameterIsolation:
    """Ensure different parameter values produce different SQL output."""

    def test_container_ready_different_namespaces(self):
        q1 = _container_ready_query("ns-a", "container")
        q2 = _container_ready_query("ns-b", "container")
        assert q1 != q2

    def test_container_ready_different_containers(self):
        q1 = _container_ready_query("ns", "container-a")
        q2 = _container_ready_query("ns", "container-b")
        assert q1 != q2

    def test_envoy_success_rate_different_patterns(self):
        q1 = _envoy_success_rate_query("pattern-a")
        q2 = _envoy_success_rate_query("pattern-b")
        assert q1 != q2

    def test_container_memory_different_deployments(self):
        q1 = _container_memory_mb_query("ns", "deploy-a")
        q2 = _container_memory_mb_query("ns", "deploy-b")
        assert q1 != q2

    @pytest.mark.parametrize(
        "query_fn",
        [
            _cnpg_up_query,
            _cnpg_backends_query,
            _cnpg_db_size_query,
            _seaweedfs_disk_query,
            _argocd_apps_synced_query,
        ],
    )
    def test_no_arg_queries_return_nonempty_string(self, query_fn):
        q = query_fn()
        assert isinstance(q, str)
        assert len(q) > 0

    @pytest.mark.parametrize(
        "query_fn",
        [
            _cnpg_up_query,
            _cnpg_backends_query,
            _cnpg_db_size_query,
            _seaweedfs_disk_query,
            _argocd_apps_synced_query,
        ],
    )
    def test_no_arg_queries_contain_select_and_from(self, query_fn):
        q = query_fn()
        assert "SELECT" in q
        assert "FROM" in q
