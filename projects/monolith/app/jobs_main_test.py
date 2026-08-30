"""Unit tests for the jobs Typer entrypoint (``app/jobs_main.py``).

These verify command dispatch only: the network (worldcup poll) and the DB are
patched, so no real session or HTTP call happens. They exist to prove the CLI
wiring stays intact as commands are added.
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

import app.jobs_main as jobs_main

runner = CliRunner()


def test_agent_drain_trigger_posts_internal_endpoint(monkeypatch):
    response = mock.Mock()
    response.json.return_value = {"status": "started"}
    monkeypatch.setenv("MONOLITH_INTERNAL_URL", "http://monolith:8000")

    with mock.patch("httpx.post", return_value=response) as post:
        result = runner.invoke(jobs_main.app, ["agent-drain-trigger"])

    assert result.exit_code == 0, result.output
    post.assert_called_once_with(
        "http://monolith:8000/internal/agent/drain", timeout=90
    )
    response.raise_for_status.assert_called_once_with()


def test_worldcup_sim_dispatches_to_refresh_handler():
    handler = mock.AsyncMock(return_value=None)
    with (
        mock.patch("core.db.get_engine", return_value=object()),
        mock.patch("sqlmodel.Session"),
        mock.patch("worldcup.jobs.refresh_handler", new=handler),
    ):
        result = runner.invoke(jobs_main.app, ["worldcup-sim"])

    assert result.exit_code == 0, result.output
    handler.assert_awaited_once()


def test_moving_gcal_sync_dispatches_to_handler():
    handler = mock.AsyncMock(return_value=None)
    with (
        mock.patch("core.db.get_engine", return_value=object()),
        mock.patch("sqlmodel.Session"),
        mock.patch("moving.gcal_sync.gcal_sync_handler", new=handler),
    ):
        result = runner.invoke(jobs_main.app, ["moving-gcal-sync"])

    assert result.exit_code == 0, result.output
    handler.assert_awaited_once()


def test_cluster_snapshot_refresh_dispatches_to_refresh():
    refresh = mock.AsyncMock(return_value=None)
    with mock.patch("home.cluster_snapshot.refresh_cluster_snapshot", new=refresh):
        result = runner.invoke(jobs_main.app, ["home-cluster-snapshot-refresh"])

    assert result.exit_code == 0, result.output
    refresh.assert_awaited_once()


def test_no_args_lists_commands():
    result = runner.invoke(jobs_main.app, [])
    # no_args_is_help exits non-zero and prints the command list.
    assert "worldcup-sim" in result.output
