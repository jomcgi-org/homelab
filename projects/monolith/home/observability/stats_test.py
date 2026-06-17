"""Tests for the public stats endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from home.observability import stats


def _mock_k8s_client():
    """Return a mocked KubernetesClient with preset counts."""
    mock = MagicMock()
    mock.count_nodes = AsyncMock(return_value=4)
    mock.count_pods = AsyncMock(return_value=135)
    mock.count_deployments = AsyncMock(return_value=64)
    mock.count_argocd_applications = AsyncMock(return_value=28)
    mock.aggregate_node_resources = AsyncMock(
        return_value={
            "cpu_used_cores": 4.987,
            "cpu_capacity_cores": 32.0,
            "memory_used_bytes": 62.5 * 1024**3,
            "memory_capacity_bytes": 108.0 * 1024**3,
        }
    )
    mock.close = AsyncMock()
    return mock


def _mock_ch_client():
    """Mock ClickHouseClient that returns canned GPU values."""
    mock = MagicMock()
    queries = {
        "DCGM_FI_DEV_GPU_UTIL": 73.5,
        "DCGM_FI_DEV_FB_USED": 18432.0,  # 18 GiB in MiB
        "DCGM_FI_DEV_FB_FREE": 6144.0,  # 6 GiB in MiB
    }

    async def query_scalar(sql):
        for marker, value in queries.items():
            if marker in sql:
                return value
        return None

    mock.query_scalar = AsyncMock(side_effect=query_scalar)
    mock.close = AsyncMock()
    return mock


def _mock_session():
    """Return a mock SQLModel Session that returns preset counts."""
    session = MagicMock()
    call_count = 0
    expected = [1309, 5948, 366]

    def exec_side_effect(query):
        nonlocal call_count
        result = MagicMock()
        result.one.return_value = (expected[call_count],)
        call_count += 1
        return result

    session.exec = MagicMock(side_effect=exec_side_effect)
    return session


@pytest.mark.asyncio
async def test_build_stats_returns_expected_shape():
    mock_client = _mock_k8s_client()
    mock_ch = _mock_ch_client()
    mock_session = _mock_session()

    with (
        patch("home.observability.stats.KubernetesClient", return_value=mock_client),
        patch("home.observability.stats.ClickHouseClient", return_value=mock_ch),
        patch("home.observability.stats.get_engine", return_value=MagicMock()),
        patch("sqlmodel.Session", return_value=mock_session),
        patch(
            "home.observability.stats._query_deploy",
            new_callable=AsyncMock,
            return_value={
                "latest_commit_sha": "abc1234",
                "latest_commit_at": "2026-04-25T10:00:00Z",
                "deployed_at": "2026-04-25T10:05:00Z",
            },
        ),
    ):
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        result = await stats.build_stats()

    assert result["cluster"]["nodes"] == 4
    assert result["cluster"]["pods"] == 135
    assert result["cluster"]["deployments"] == 64
    assert result["cluster"]["argocd_apps"] == 28
    assert result["cluster"]["cpu_used_cores"] == 4.99
    assert result["cluster"]["cpu_capacity_cores"] == 32.0
    assert result["cluster"]["memory_used_gb"] == 62.5
    assert result["cluster"]["memory_capacity_gb"] == 108.0
    assert result["gpu"]["utilization_pct"] == 73.5
    assert result["gpu"]["memory_used_gb"] == 18.0
    assert result["gpu"]["memory_total_gb"] == 24.0
    assert result["knowledge"]["facts"] == 1309
    assert result["knowledge"]["chunks"] == 5948
    assert result["knowledge"]["raw_inputs"] == 366
    assert result["deploy"]["latest_commit_sha"] == "abc1234"
    assert result["deploy"]["deployed_at"] == "2026-04-25T10:05:00Z"
    assert result["platform"]["in_production_since"] == "2025-01"
    assert "cached_at" in result


@pytest.mark.asyncio
async def test_query_deploy_combines_github_and_argocd():
    commit_payload = {
        "sha": "abcdef1234567890",
        "commit": {"committer": {"date": "2026-04-25T10:00:00Z"}},
    }
    mock_resp = MagicMock(status_code=200)
    mock_resp.json = MagicMock(return_value=commit_payload)
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=mock_resp)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    mock_k8s = MagicMock()
    mock_k8s.get_argocd_app_status = AsyncMock(
        return_value={"operationState": {"finishedAt": "2026-04-25T10:05:00Z"}}
    )
    mock_k8s.close = AsyncMock()

    with (
        patch("home.observability.stats.httpx.AsyncClient", return_value=mock_http),
        patch("home.observability.stats.KubernetesClient", return_value=mock_k8s),
    ):
        result = await stats._query_deploy()

    assert result["latest_commit_sha"] == "abcdef1"
    assert result["latest_commit_at"] == "2026-04-25T10:00:00Z"
    assert result["deployed_at"] == "2026-04-25T10:05:00Z"


@pytest.mark.asyncio
async def test_query_deploy_returns_partial_when_one_source_fails():
    """If GitHub is down but ArgoCD answers, the deployed_at item still surfaces."""
    mock_resp = MagicMock(status_code=503)
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=mock_resp)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    mock_k8s = MagicMock()
    mock_k8s.get_argocd_app_status = AsyncMock(
        return_value={"operationState": {"finishedAt": "2026-04-25T10:05:00Z"}}
    )
    mock_k8s.close = AsyncMock()

    with (
        patch("home.observability.stats.httpx.AsyncClient", return_value=mock_http),
        patch("home.observability.stats.KubernetesClient", return_value=mock_k8s),
    ):
        result = await stats._query_deploy()

    assert "latest_commit_sha" not in result
    assert result["deployed_at"] == "2026-04-25T10:05:00Z"


@pytest.mark.asyncio
async def test_cluster_counts_handles_k8s_errors():
    mock_client = _mock_k8s_client()
    mock_client.count_nodes = AsyncMock(side_effect=Exception("k8s unreachable"))
    mock_client.count_pods = AsyncMock(return_value=10)
    mock_client.count_deployments = AsyncMock(return_value=5)
    mock_client.count_argocd_applications = AsyncMock(return_value=2)
    # Keep a reference to the exception so we can verify it is forwarded to logger.warning
    # as a positional arg (not as exc_info=), which is the correct pattern outside an
    # except block.
    resources_error = Exception("metrics-server unreachable")
    mock_client.aggregate_node_resources = AsyncMock(side_effect=resources_error)

    with (
        patch("home.observability.stats.KubernetesClient", return_value=mock_client),
        patch("home.observability.stats.logger") as mock_logger,
    ):
        result = await stats._query_cluster_counts()

    assert result["nodes"] == 0  # failed, falls back to 0
    assert result["pods"] == 10
    # When aggregate_node_resources fails, the resource keys are simply absent.
    assert "cpu_used_cores" not in result
    assert "memory_used_gb" not in result
    # Regression guard: exc_info must be False (not the exception object).
    # Passing an Exception instance as exc_info outside an except block is a bug —
    # the original code had exc_info=resources which was wrong; it was fixed to
    # exc_info=False. This assertion ensures that regression cannot silently creep back.
    mock_logger.warning.assert_called_once_with(
        "Node resource aggregation failed: %s",
        resources_error,
        exc_info=False,
    )


# ---------------------------------------------------------------------------
# _query_gpu
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_gpu_returns_correct_values():
    """Happy path: utilization_pct + memory values divided by 1024, rounded."""
    mock_ch = _mock_ch_client()

    with patch("home.observability.stats.ClickHouseClient", return_value=mock_ch):
        result = await stats._query_gpu()

    assert result["utilization_pct"] == 73.5
    # 18432 MiB / 1024 = 18.0 GiB
    assert result["memory_used_gb"] == 18.0
    # (18432 + 6144) MiB / 1024 = 24.0 GiB
    assert result["memory_total_gb"] == 24.0


@pytest.mark.asyncio
async def test_query_gpu_memory_rounding():
    """memory_used_gb and memory_total_gb are rounded to 1 decimal place."""
    mock_ch = MagicMock()

    # 1100 MiB used, 500 MiB free
    # used_gb = round(1100/1024, 1) = round(1.07421..., 1) = 1.1
    # total_gb = round(1600/1024, 1) = round(1.5625, 1) = 1.6
    async def query_scalar(sql):
        if "DCGM_FI_DEV_GPU_UTIL" in sql:
            return 55.0
        if "DCGM_FI_DEV_FB_USED" in sql:
            return 1100.0
        if "DCGM_FI_DEV_FB_FREE" in sql:
            return 500.0
        return None

    mock_ch.query_scalar = AsyncMock(side_effect=query_scalar)
    mock_ch.close = AsyncMock()

    with patch("home.observability.stats.ClickHouseClient", return_value=mock_ch):
        result = await stats._query_gpu()

    assert result["memory_used_gb"] == round(1100 / 1024, 1)
    assert result["memory_total_gb"] == round(1600 / 1024, 1)


@pytest.mark.asyncio
async def test_query_gpu_partial_failure_omits_memory():
    """If one memory query raises, memory keys are absent but utilization_pct is kept."""
    mock_ch = MagicMock()

    async def query_scalar(sql):
        if "DCGM_FI_DEV_GPU_UTIL" in sql:
            return 60.0
        if "DCGM_FI_DEV_FB_USED" in sql:
            raise RuntimeError("ClickHouse timeout")
        if "DCGM_FI_DEV_FB_FREE" in sql:
            return 4096.0
        return None

    mock_ch.query_scalar = AsyncMock(side_effect=query_scalar)
    mock_ch.close = AsyncMock()

    with patch("home.observability.stats.ClickHouseClient", return_value=mock_ch):
        result = await stats._query_gpu()

    # utilization_pct should still be present from the successful query
    assert result["utilization_pct"] == 60.0
    # memory keys require both fb_used and fb_free — partial failure drops them
    assert "memory_used_gb" not in result
    assert "memory_total_gb" not in result


@pytest.mark.asyncio
async def test_query_gpu_total_failure_returns_none_utilization():
    """If the outer ClickHouseClient construction itself explodes, returns sentinel."""
    with patch(
        "home.observability.stats.ClickHouseClient",
        side_effect=Exception("connection refused"),
    ):
        result = await stats._query_gpu()

    assert result == {"utilization_pct": None}


@pytest.mark.asyncio
async def test_query_gpu_all_queries_fail_returns_none_utilization():
    """If every scalar query raises, utilization_pct is None and memory absent."""
    mock_ch = MagicMock()
    mock_ch.query_scalar = AsyncMock(side_effect=Exception("ClickHouse unavailable"))
    mock_ch.close = AsyncMock()

    with patch("home.observability.stats.ClickHouseClient", return_value=mock_ch):
        result = await stats._query_gpu()

    assert result["utilization_pct"] is None
    assert "memory_used_gb" not in result
    assert "memory_total_gb" not in result


# ---------------------------------------------------------------------------
# _query_argocd_monolith_deploy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_argocd_monolith_deploy_returns_expected_dict():
    """Returns {"finished_at": <timestamp>} when operationState is present."""
    mock_k8s = MagicMock()
    mock_k8s.get_argocd_app_status = AsyncMock(
        return_value={
            "operationState": {"finishedAt": "2026-04-25T10:05:00Z"},
            "health": {"status": "Healthy"},
        }
    )
    mock_k8s.close = AsyncMock()

    with patch("home.observability.stats.KubernetesClient", return_value=mock_k8s):
        result = await stats._query_argocd_monolith_deploy()

    assert result == {"finished_at": "2026-04-25T10:05:00Z"}
    mock_k8s.get_argocd_app_status.assert_called_once_with(stats.ARGOCD_APP_NAME)


@pytest.mark.asyncio
async def test_query_argocd_monolith_deploy_returns_none_when_no_status():
    """Returns None if get_argocd_app_status returns falsy."""
    mock_k8s = MagicMock()
    mock_k8s.get_argocd_app_status = AsyncMock(return_value=None)
    mock_k8s.close = AsyncMock()

    with patch("home.observability.stats.KubernetesClient", return_value=mock_k8s):
        result = await stats._query_argocd_monolith_deploy()

    assert result is None


@pytest.mark.asyncio
async def test_query_argocd_monolith_deploy_returns_none_when_no_finished_at():
    """Returns None if operationState exists but has no finishedAt field."""
    mock_k8s = MagicMock()
    mock_k8s.get_argocd_app_status = AsyncMock(
        return_value={"operationState": {"phase": "Running"}}
    )
    mock_k8s.close = AsyncMock()

    with patch("home.observability.stats.KubernetesClient", return_value=mock_k8s):
        result = await stats._query_argocd_monolith_deploy()

    assert result is None


@pytest.mark.asyncio
async def test_query_argocd_monolith_deploy_returns_none_on_exception():
    """Returns None (does not raise) if the Kubernetes call fails."""
    mock_k8s = MagicMock()
    mock_k8s.get_argocd_app_status = AsyncMock(
        side_effect=Exception("k8s API unavailable")
    )
    mock_k8s.close = AsyncMock()

    with patch("home.observability.stats.KubernetesClient", return_value=mock_k8s):
        result = await stats._query_argocd_monolith_deploy()

    assert result is None
