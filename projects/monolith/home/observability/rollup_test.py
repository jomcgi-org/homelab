"""Tests for the observability rollup jobs (ADR 004 Layer 4)."""

import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from home.observability import rollup


@pytest.mark.asyncio
async def test_stats_rollup_builds_then_writes():
    payload = {
        "cluster": {"nodes": 4},
        "knowledge": {"facts": 1309},
        "deploy": {"latest_commit_sha": "abc1234"},
        "platform": {"in_production_since": "2025-01"},
        "cached_at": "2026-10-18T00:00:00Z",
    }
    event_loop_thread = threading.get_ident()
    engine = MagicMock()
    session = MagicMock()
    session.__enter__.return_value = session

    def open_session(actual_engine):
        # The real writer must open and own its session off the event loop.
        assert threading.get_ident() != event_loop_thread
        assert actual_engine is engine
        return session

    with (
        patch(
            "home.observability.rollup.build_stats",
            new_callable=AsyncMock,
            return_value=payload,
        ) as mock_build,
        patch("home.observability.rollup.get_engine", return_value=engine),
        patch(
            "home.observability.rollup.Session", side_effect=open_session
        ) as mock_session,
    ):
        await rollup.stats_rollup()
    mock_build.assert_awaited_once()
    mock_session.assert_called_once_with(engine)
    session.execute.assert_called_once()
    query, params = session.execute.call_args.args
    assert "INSERT INTO observability.stats_snapshot" in str(query)
    assert "ON CONFLICT (id) DO UPDATE" in str(query)
    assert json.loads(params["payload"]) == payload
    assert "gpu" not in json.loads(params["payload"])
    session.commit.assert_called_once_with()
    session.__exit__.assert_called_once()


@pytest.mark.asyncio
async def test_prime_snapshots_swallows_errors():
    with patch(
        "home.observability.rollup.stats_rollup",
        new_callable=AsyncMock,
        side_effect=Exception("snapshot database unavailable"),
    ):
        # Must not raise: a failed prime is logged and the Argo rollup retries.
        await rollup.prime_snapshots()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [None, "true", "false", "0", " NO "])
async def test_home_module_startup_preserves_snapshot_priming(monkeypatch, enabled):
    from home.module import MODULE

    if enabled is None:
        monkeypatch.delenv("HOME_OBSERVABILITY_PRIME_ENABLED", raising=False)
    else:
        monkeypatch.setenv("HOME_OBSERVABILITY_PRIME_ENABLED", enabled)
    with patch.object(rollup, "prime_snapshots", new_callable=AsyncMock) as prime:
        assert MODULE.startup is not None
        await MODULE.startup(None)
    if enabled in (None, "true"):
        prime.assert_awaited_once_with()
    else:
        prime.assert_not_awaited()
