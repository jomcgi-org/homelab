"""Unit tests for scheduler/router.py — /api/scheduler endpoints."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.db import get_session
import dataclasses
import scheduler.module
from framework import PRIVATE_PROFILE, build_app
from scheduler.service import RunNowResult
from scheduler.views import SchedulerJobView

# Compose only the scheduler domain instead of the whole monolith: the
# same framework wiring the production app gets, without depending on
# the app composition root, which imports every other domain.
app = build_app(
    dataclasses.replace(PRIVATE_PROFILE, otel_enabled=False),
    (scheduler.module.MODULE,),
)


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
    def test_matching_cronworkflow_returns_202(self, client, fake_session):
        with patch(
            "scheduler.router.service.run_now",
            return_value=RunNowResult(
                job="j",
                workflow_name="nightly-manual-abc12",
                namespace="workflows-test",
                status_code=202,
            ),
        ) as mock_trigger:
            r = client.post("/api/scheduler/jobs/j/run-now")

        assert r.status_code == 202
        assert r.json() == {
            "job": "j",
            "workflow_name": "nightly-manual-abc12",
            "namespace": "workflows-test",
        }
        mock_trigger.assert_awaited_once_with(fake_session, "j")

    def test_returns_404_for_missing(self, client):
        with patch(
            "scheduler.router.service.run_now",
            side_effect=HTTPException(status_code=404, detail="unknown job: missing"),
        ):
            r = client.post("/api/scheduler/jobs/missing/run-now")
        assert r.status_code == 404

    def test_returns_409_when_no_cronworkflow_matches(self, client):
        with patch(
            "scheduler.router.service.run_now",
            return_value=RunNowResult(
                job="j",
                workflow_name=None,
                namespace="workflows-test",
                status_code=409,
                message="no CronWorkflow replaces job j",
            ),
        ):
            r = client.post("/api/scheduler/jobs/j/run-now")

        assert r.status_code == 409
        assert r.json()["detail"] == "no CronWorkflow replaces job j"

    def test_returns_502_for_kubernetes_error(self, client):
        with patch(
            "scheduler.router.service.run_now",
            return_value=RunNowResult(
                job="j",
                workflow_name=None,
                namespace="workflows-test",
                status_code=502,
                message="Kubernetes API error: boom",
            ),
        ):
            r = client.post("/api/scheduler/jobs/j/run-now")

        assert r.status_code == 502
        assert r.json()["detail"] == "Kubernetes API error: boom"
