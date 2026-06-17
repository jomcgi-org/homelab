"""Tests for the observability rollup jobs (ADR 004 Layer 4)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from home.observability import rollup


@pytest.mark.asyncio
async def test_topology_rollup_builds_then_writes():
    payload = {"nodes": [{"id": "a"}], "groups": [], "edges": []}
    with (
        patch(
            "home.observability.rollup.build_topology",
            new_callable=AsyncMock,
            return_value=payload,
        ) as mock_build,
        patch("home.observability.rollup._write_topology_snapshot") as mock_write,
    ):
        await rollup.topology_rollup()
    mock_build.assert_awaited_once()
    mock_write.assert_called_once_with(payload)


@pytest.mark.asyncio
async def test_stats_rollup_builds_then_writes():
    payload = {"cluster": {"nodes": 4}}
    with (
        patch(
            "home.observability.rollup.build_stats",
            new_callable=AsyncMock,
            return_value=payload,
        ) as mock_build,
        patch("home.observability.rollup._write_stats_snapshot") as mock_write,
    ):
        await rollup.stats_rollup()
    mock_build.assert_awaited_once()
    mock_write.assert_called_once_with(payload)


def test_register_registers_both_rollup_jobs():
    session = MagicMock()
    with patch("home.observability.rollup.register_job") as mock_register:
        rollup.register(session)
    names = {call.kwargs["name"] for call in mock_register.call_args_list}
    assert names == {"observability.topology_rollup", "observability.stats_rollup"}


@pytest.mark.asyncio
async def test_prime_snapshots_swallows_errors():
    with (
        patch(
            "home.observability.rollup.topology_rollup",
            new_callable=AsyncMock,
            side_effect=Exception("clickhouse down"),
        ),
        patch("home.observability.rollup.stats_rollup", new_callable=AsyncMock),
    ):
        # Must not raise: a failed prime is logged and the scheduler retries.
        await rollup.prime_snapshots()
