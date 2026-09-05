"""Tests for the scheduler CLI subcommand."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from tools.cli.main import app


@pytest.fixture
def runner():
    return CliRunner()


def _mock_response(data, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_redirect = False
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


JOB_A = {
    "name": "knowledge.gardener",
    "interval_secs": 600,
    "ttl_secs": 300,
    "next_run_at": "2026-04-25T14:18:00+00:00",
    "last_run_at": "2026-04-25T14:08:00+00:00",
    "last_status": "ok",
    "has_handler": True,
}

JOB_B = {
    "name": "home.calendar_poll",
    "interval_secs": 900,
    "ttl_secs": 120,
    "next_run_at": "2026-04-25T14:32:00+00:00",
    "last_run_at": None,
    "last_status": None,
    "has_handler": True,
}


class TestListJobs:
    def test_lists_jobs(self, runner):
        resp = _mock_response([JOB_A, JOB_B])
        with patch("tools.cli.scheduler_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["scheduler", "jobs", "list"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with("get", "/api/scheduler/jobs")
        assert "knowledge.gardener" in result.output
        assert "home.calendar_poll" in result.output
        assert "every   600s" in result.output
        assert "last ok at 14:08" in result.output
        assert "never run" in result.output

    def test_lists_empty(self, runner):
        resp = _mock_response([])
        with patch("tools.cli.scheduler_cmd._request", return_value=resp):
            result = runner.invoke(app, ["scheduler", "jobs", "list"])

        assert result.exit_code == 0
        assert "No jobs registered." in result.output

    def test_json_output(self, runner):
        resp = _mock_response([JOB_A])
        with patch("tools.cli.scheduler_cmd._request", return_value=resp):
            result = runner.invoke(app, ["scheduler", "jobs", "list", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed[0]["name"] == "knowledge.gardener"


class TestGetJob:
    def test_returns_existing(self, runner):
        resp = _mock_response(JOB_A)
        with patch("tools.cli.scheduler_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(
                app, ["scheduler", "jobs", "get", "knowledge.gardener"]
            )

        assert result.exit_code == 0
        mock_req.assert_called_once_with(
            "get", "/api/scheduler/jobs/knowledge.gardener"
        )
        assert "knowledge.gardener" in result.output

    def test_404_exits_nonzero(self, runner):
        resp = _mock_response({"detail": "unknown job: nope"}, status_code=404)
        with patch("tools.cli.scheduler_cmd._request", return_value=resp):
            result = runner.invoke(app, ["scheduler", "jobs", "get", "nope"])

        assert result.exit_code == 1
        assert "Unknown job: nope" in result.output


class TestRunNow:
    def test_triggers_job(self, runner):
        resp = _mock_response(
            {
                "job": "knowledge.gardener",
                "workflow_name": "knowledge-gardener-manual-abc12",
                "namespace": "monolith-workflows",
            },
            status_code=202,
        )
        with patch("tools.cli.scheduler_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(
                app, ["scheduler", "jobs", "run-now", "knowledge.gardener"]
            )

        assert result.exit_code == 0
        mock_req.assert_called_once_with(
            "post", "/api/scheduler/jobs/knowledge.gardener/run-now"
        )
        assert (
            "Submitted knowledge-gardener-manual-abc12 in monolith-workflows "
            "for knowledge.gardener."
        ) in result.output

    def test_404_exits_nonzero(self, runner):
        resp = _mock_response({"detail": "unknown job"}, status_code=404)
        with patch("tools.cli.scheduler_cmd._request", return_value=resp):
            result = runner.invoke(app, ["scheduler", "jobs", "run-now", "nope"])

        assert result.exit_code == 1
        assert "Unknown job: nope" in result.output


class TestSchedulerLineFormatter:
    def test_orphan_marker_when_handler_missing(self, runner):
        orphan = {**JOB_A, "has_handler": False}
        resp = _mock_response([orphan])
        with patch("tools.cli.scheduler_cmd._request", return_value=resp):
            result = runner.invoke(app, ["scheduler", "jobs", "list"])

        assert result.exit_code == 0
        assert "[orphan]" in result.output
