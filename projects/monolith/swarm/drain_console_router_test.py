import json
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import swarm.drain_console_router as drain_console_router
from agent.config import DrainerSettings

NOW = datetime.now(timezone.utc)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(drain_console_router.router)
    return TestClient(app)


def _settings() -> DrainerSettings:
    return DrainerSettings(
        enabled=True,
        max_jobs_per_cycle=3,
        turn_timeout_seconds=1800,
        stall_threshold_seconds=2700,
        job_kind="qwen-drain",
        repo="jomcgi/homelab",
        branch="main",
        reasoning=True,
    )


def _job(**overrides):
    job = {
        "name": "qd-a",
        "routine_kind": "qwen-drain",
        "interval_secs": None,
        "payload": {"prompt": "Audit"},
        "next_run_at": None,
        "last_run_at": NOW - timedelta(minutes=10),
        "last_status": "error",
        "last_summary": "turn timed out after 1800 seconds",
        "locked_by": None,
        "locked_at": None,
        "ttl_secs": 2100,
        "created_by": "joe",
        "created_at": NOW - timedelta(hours=3),
    }
    job.update(overrides)
    return job


def _patch_common(monkeypatch, jobs, sessions=None, cycles=None, stats=None):
    monkeypatch.setattr(drain_console_router, "load_drainer_settings", _settings)
    monkeypatch.setattr(drain_console_router, "list_jobs", lambda kind: jobs)
    monkeypatch.setattr(
        drain_console_router, "_load_drainer_sessions", lambda limit=0: sessions or []
    )
    monkeypatch.setattr(drain_console_router, "_load_last_turns", lambda ids: {})
    monkeypatch.setattr(drain_console_router, "_load_partials", lambda ids: {})
    monkeypatch.setattr(
        drain_console_router, "_load_cycles", lambda limit=0: cycles or []
    )
    monkeypatch.setattr(
        drain_console_router, "_load_step_stats", lambda ids: stats or {}
    )
    monkeypatch.setattr(drain_console_router, "_server_app_version", lambda: "v1")


def test_console_composes_jobs_and_lane(monkeypatch):
    cycle = {
        "workflow_uuid": "wf-live",
        "status": "PENDING",
        "created_at": int((NOW - timedelta(minutes=5)).timestamp() * 1000),
        "updated_at": int((NOW - timedelta(minutes=5)).timestamp() * 1000),
        "application_version": "v1",
    }
    stats = {
        "wf-live": {
            "last_ms": int((NOW - timedelta(seconds=5)).timestamp() * 1000),
            "steps": 60,
            "claims": 1,
            "finishes": 0,
            "last_step": "claim_drainer_job",
        }
    }
    _patch_common(monkeypatch, [_job()], cycles=[cycle], stats=stats)

    response = _client().get("/api/agents/drain/console")

    assert response.status_code == 200
    body = response.json()
    assert body["lane"]["state"] == "running"
    assert body["lane"]["reap_after_seconds"] == 1800 + 3 * 60 + 600
    assert body["queue"]["error"] == 1
    assert body["jobs"][0]["name"] == "qd-a"
    assert body["jobs"][0]["state"] == "error"


def test_console_survives_dbos_read_failure(monkeypatch):
    _patch_common(monkeypatch, [_job()])

    def boom(limit=0):
        raise RuntimeError("no dbos schema")

    monkeypatch.setattr(drain_console_router, "_load_cycles", boom)

    response = _client().get("/api/agents/drain/console")

    assert response.status_code == 200
    body = response.json()
    assert body["lane"]["state"] == "unknown"
    assert body["lane"]["error"] == "cycle state unavailable"
    # The job queue still renders: the two truths are independent.
    assert body["jobs"][0]["name"] == "qd-a"


def test_console_list_never_calls_github(monkeypatch):
    job = _job(
        last_status="ok",
        last_summary="https://github.com/jomcgi-org/homelab/pull/456",
    )
    _patch_common(monkeypatch, [job])

    async def unexpected_github_call(url):
        raise AssertionError(f"list endpoint called GitHub: {url}")

    monkeypatch.setattr(drain_console_router, "_github_get", unexpected_github_call)

    response = _client().get("/api/agents/drain/console")

    assert response.status_code == 200
    assert response.json()["jobs"][0]["pr"]["number"] == 456


def test_job_detail_joins_attempts(monkeypatch):
    sessions = [
        {
            "id": 12,
            "local_session_id": "wf2:qwen-drain:qd-a",
            "workflow_id": "wf2",
            "status": "completed",
            "created_at": NOW - timedelta(minutes=12),
        },
        {
            "id": 9,
            "local_session_id": "wf1:qwen-drain:qd-other",
            "workflow_id": "wf1",
            "status": "completed",
            "created_at": NOW - timedelta(hours=1),
        },
    ]
    _patch_common(monkeypatch, [_job()], sessions=sessions)

    class _Turn:
        seq = 1
        result_text = "did the thing"
        terminal_reason = "stop"
        cost_usd = 0.0
        created_at = NOW - timedelta(minutes=11)
        usage_json = '{"activities": [{"type": "bash", "command": "git log"}]}'

    monkeypatch.setattr(
        drain_console_router, "_load_turns_for_sessions", lambda ids: {12: _Turn()}
    )

    response = _client().get("/api/agents/drain/jobs/qd-a")

    assert response.status_code == 200
    body = response.json()
    assert body["job"]["name"] == "qd-a"
    assert body["job"]["prompt"] == "Audit"
    assert len(body["attempts"]) == 1
    attempt = body["attempts"][0]
    assert attempt["session_id"] == 12
    assert attempt["calls"] == 1
    assert attempt["activities"] == [{"type": "bash", "command": "git log"}]
    assert attempt["turn"]["terminal_reason"] == "stop"


def test_job_detail_pr_enrichment_fails_soft(monkeypatch):
    job = _job(
        last_status="ok",
        last_summary="https://github.com/jomcgi/homelab/pull/123",
    )
    _patch_common(monkeypatch, [job])
    monkeypatch.setattr(drain_console_router, "_PR_CACHE", {})

    def github_down(url):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(drain_console_router, "_github_get", github_down)

    response = _client().get("/api/agents/drain/jobs/qd-a")

    assert response.status_code == 200
    assert response.json()["job"]["pr"] == {
        "url": "https://github.com/jomcgi/homelab/pull/123",
        "number": 123,
        "repo": "jomcgi/homelab",
    }


def test_job_detail_caches_successful_pr_enrichment(monkeypatch):
    job = _job(
        last_status="ok",
        last_summary="https://github.com/jomcgi-org/homelab/pull/456",
    )
    _patch_common(monkeypatch, [job])
    monkeypatch.setattr(drain_console_router, "_PR_CACHE", {})
    calls = []

    def github_get(url):
        calls.append(url)
        return httpx.Response(
            200,
            json={
                "title": "Classify drain outcomes",
                "state": "closed",
                "merged": True,
                "changed_files": 3,
                "additions": 15,
                "deletions": 8,
            },
        )

    monkeypatch.setattr(drain_console_router, "_github_get", github_get)

    first = _client().get("/api/agents/drain/jobs/qd-a")
    second = _client().get("/api/agents/drain/jobs/qd-a")

    assert first.status_code == 200
    assert len(calls) == 1
    assert second.json()["job"]["pr"] == first.json()["job"]["pr"]
    assert calls == [
        f"{drain_console_router.GITHUB_API}/repos/"
        f"{drain_console_router.GITHUB_REPO}/pulls/456"
    ]
    assert first.json()["job"]["pr"]["title"] == "Classify drain outcomes"
    assert first.json()["job"]["pr"]["merged"] is True


def test_job_detail_unknown_404(monkeypatch):
    _patch_common(monkeypatch, [_job()])
    response = _client().get("/api/agents/drain/jobs/nope")
    assert response.status_code == 404


def test_requeue_refuses_live_lock(monkeypatch):
    live = _job(locked_by="qwen-drainer", locked_at=NOW - timedelta(seconds=30))
    _patch_common(monkeypatch, [live])

    response = _client().post("/api/agents/drain/jobs/qd-a/requeue")

    assert response.status_code == 409


def test_requeue_rearms_dead_one_shot(monkeypatch):
    _patch_common(monkeypatch, [_job()])
    triggered = []

    import agent.routine_jobs as routine_jobs

    monkeypatch.setattr(
        routine_jobs, "trigger_job", lambda name: triggered.append(name) or True
    )

    response = _client().post("/api/agents/drain/jobs/qd-a/requeue")

    assert response.status_code == 200
    assert response.json() == {"requeued": True, "name": "qd-a"}
    assert triggered == ["qd-a"]


def test_requeue_unknown_404(monkeypatch):
    _patch_common(monkeypatch, [_job()])
    response = _client().post("/api/agents/drain/jobs/missing/requeue")
    assert response.status_code == 404


def test_activity_cap_keeps_the_tail():
    activities = [{"command": f"cmd {i}"} for i in range(700)]
    capped, total = drain_console_router._capped_activities(
        json.dumps({"activities": activities})
    )
    assert total == 700
    assert len(capped) == drain_console_router._ACTIVITY_LIST_CAP
    # The tail is what shows the loop a runaway is stuck in.
    assert capped[-1]["command"] == "cmd 699"
    assert capped[0]["command"] == "cmd 200"
