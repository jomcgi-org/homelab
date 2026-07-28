"""Unit tests for scheduler/router.py — /api/scheduler endpoints."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.db import get_session
from app.main import app
from scheduler.views import SchedulerJobView


def _view(name: str = "j", *, has_handler: bool = True) -> SchedulerJobView:
    return SchedulerJobView(
        name=name,
        interval_secs=60,
        ttl_secs=300,
        next_run_at=datetime(2026, 4, 25, 14, 0, 0, tzinfo=timezone.utc),
        last_run_at=datetime(2026, 4, 25, 13, 59, 0, tzinfo=timezone.utc),
        last_status="ok",
        has_handler=has_handler,
    )


@pytest.fixture()
def fake_session():
    return MagicMock()


@pytest.fixture()
def client(fake_session):
    app.dependency_overrides[get_session] = lambda: fake_session
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestListJobs:
    def test_returns_jobs(self, client):
        with patch(
            "scheduler.router.service.list_jobs",
            return_value=[_view("a"), _view("b")],
        ):
            r = client.get("/api/scheduler/jobs")
        assert r.status_code == 200
        body = r.json()
        assert [j["name"] for j in body] == ["a", "b"]
        # Lock columns must not leak onto the wire.
        assert "locked_by" not in body[0]
        assert "locked_at" not in body[0]

    def test_returns_empty(self, client):
        with patch("scheduler.router.service.list_jobs", return_value=[]):
            r = client.get("/api/scheduler/jobs")
        assert r.status_code == 200
        assert r.json() == []


class TestGetJob:
    def test_returns_existing_job(self, client):
        with patch("scheduler.router.service.get_job", return_value=_view("j")):
            r = client.get("/api/scheduler/jobs/j")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "j"

    def test_returns_404_for_missing(self, client):
        with patch("scheduler.router.service.get_job", return_value=None):
            r = client.get("/api/scheduler/jobs/missing")
        assert r.status_code == 404
        body = r.json()
        assert "missing" in body["detail"]


class TestRunNow:
    def test_returns_view_after_trigger(self, client):
        with patch(
            "scheduler.router.service.mark_for_immediate_run",
            return_value=_view("j"),
        ) as mock_trigger:
            r = client.post("/api/scheduler/jobs/j/run-now")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "j"
        mock_trigger.assert_called_once()

    def test_returns_404_for_missing(self, client):
        with patch(
            "scheduler.router.service.mark_for_immediate_run",
            return_value=None,
        ):
            r = client.post("/api/scheduler/jobs/missing/run-now")
        assert r.status_code == 404
