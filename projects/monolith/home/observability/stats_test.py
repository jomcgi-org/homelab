"""Tests for the public stats endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_dcgm(handler):
    """Patch the stats client to use an httpx MockTransport."""
    transport = httpx.MockTransport(handler)
    return patch.object(
        stats.httpx,
        "AsyncClient",
        side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
    )


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
    mock_session = _mock_session()

    with (
        patch("home.observability.stats.KubernetesClient", return_value=mock_client),
        patch(
            "home.observability.stats._query_gpu",
            new_callable=AsyncMock,
            return_value={
                "utilization_pct": 73.5,
                "memory_used_gb": 18.0,
                "memory_total_gb": 24.0,
            },
        ),
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
    """One GPU produces the exact ticker keys and rounded values."""
    body = """\
# HELP DCGM_FI_DEV_GPU_UTIL GPU utilization.
# TYPE DCGM_FI_DEV_GPU_UTIL gauge
DCGM_FI_DEV_GPU_UTIL{gpu="0",modelName="NVIDIA GeForce RTX 4090"} 73.54
DCGM_FI_DEV_FB_USED{gpu="0"} 18432.2
DCGM_FI_DEV_FB_FREE{gpu="0"} 6143.6
"""

    def handler(request):
        assert request.url == "http://dcgm.test:9400/metrics"
        return httpx.Response(200, text=body)

    with (
        patch.dict("os.environ", {"DCGM_EXPORTER_URL": "http://dcgm.test:9400"}),
        _mock_dcgm(handler),
    ):
        result = await stats._query_gpu()

    assert result == {
        "utilization_pct": 73.5,
        "memory_used_gb": 18.0,
        "memory_total_gb": 24.0,
    }


@pytest.mark.asyncio
async def test_query_gpu_averages_two_gpus_before_rounding():
    body = """\
DCGM_FI_DEV_GPU_UTIL{gpu="0"} 10.04
DCGM_FI_DEV_GPU_UTIL{gpu="1"} 20.07
DCGM_FI_DEV_FB_USED{gpu="0"} 1100.2
DCGM_FI_DEV_FB_USED{gpu="1"} 1101.6
DCGM_FI_DEV_FB_FREE{gpu="0"} 500.2
DCGM_FI_DEV_FB_FREE{gpu="1"} 501.6
DCGM_FI_DEV_FB_USED_TOTAL{gpu="0"} 999999
"""

    with (
        patch.dict("os.environ", {"DCGM_EXPORTER_URL": "http://dcgm.test:9400"}),
        _mock_dcgm(lambda request: httpx.Response(200, text=body)),
    ):
        result = await stats._query_gpu()

    assert result == {
        "utilization_pct": 15.1,
        "memory_used_gb": 1.1,
        "memory_total_gb": 1.6,
    }


@pytest.mark.asyncio
async def test_query_gpu_skips_help_and_type_lines():
    body = """\
# HELP DCGM_FI_DEV_GPU_UTIL GPU utilization.
# TYPE DCGM_FI_DEV_GPU_UTIL gauge
DCGM_FI_DEV_GPU_UTIL 40
# HELP DCGM_FI_DEV_FB_USED Frame buffer used.
# TYPE DCGM_FI_DEV_FB_USED gauge
DCGM_FI_DEV_FB_USED 2048
# HELP DCGM_FI_DEV_FB_FREE Frame buffer free.
# TYPE DCGM_FI_DEV_FB_FREE gauge
DCGM_FI_DEV_FB_FREE 6144
"""

    with (
        patch.dict("os.environ", {"DCGM_EXPORTER_URL": "http://dcgm.test:9400"}),
        _mock_dcgm(lambda request: httpx.Response(200, text=body)),
    ):
        result = await stats._query_gpu()

    assert result == {
        "utilization_pct": 40.0,
        "memory_used_gb": 2.0,
        "memory_total_gb": 8.0,
    }


@pytest.mark.asyncio
async def test_query_gpu_missing_metric_omits_memory_keys():
    body = """\
DCGM_FI_DEV_GPU_UTIL{gpu="0"} 60
DCGM_FI_DEV_FB_FREE{gpu="0"} 4096
"""

    with (
        patch.dict("os.environ", {"DCGM_EXPORTER_URL": "http://dcgm.test:9400"}),
        _mock_dcgm(lambda request: httpx.Response(200, text=body)),
    ):
        result = await stats._query_gpu()

    assert result == {"utilization_pct": 60.0}


@pytest.mark.asyncio
async def test_query_gpu_non_200_returns_none_utilization():
    with (
        patch.dict("os.environ", {"DCGM_EXPORTER_URL": "http://dcgm.test:9400"}),
        _mock_dcgm(lambda request: httpx.Response(503, text="unavailable")),
    ):
        result = await stats._query_gpu()

    assert result == {"utilization_pct": None}


@pytest.mark.asyncio
async def test_query_gpu_connection_error_returns_none_utilization():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with (
        patch.dict("os.environ", {"DCGM_EXPORTER_URL": "http://dcgm.test:9400"}),
        _mock_dcgm(handler),
    ):
        result = await stats._query_gpu()

    assert result == {"utilization_pct": None}


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
