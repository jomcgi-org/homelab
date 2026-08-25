from __future__ import annotations

import swarm.drainer as drainer


SETTINGS = {
    "enabled": True,
    "max_jobs_per_cycle": 3,
    "turn_timeout_seconds": 1800,
    "job_kind": "qwen-drain",
    "repo": "jomcgi-org/homelab",
    "branch": "main",
}


class FakeDBOS:
    workflow_id = "workflow-1"


def _run(monkeypatch, jobs, await_turn=None, start_session=None):
    claims = []
    starts = []
    completions = []
    notifications = []
    destroys = []
    queued = iter([*jobs, None])

    monkeypatch.setattr(drainer.agent_config, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer, "pin_drainer_settings", lambda: SETTINGS.copy())
    monkeypatch.setattr(drainer, "DBOS", FakeDBOS)

    def claim(ttl_secs, kind):
        claims.append((ttl_secs, kind))
        return next(queued)

    def start(*args):
        starts.append(args)
        if start_session is not None:
            return start_session(*args)
        return 100 + len(starts)

    monkeypatch.setattr(drainer, "claim_drainer_job", claim)
    monkeypatch.setattr(drainer, "start_drainer_session", start)
    monkeypatch.setattr(
        drainer,
        "_await_turn",
        await_turn
        or (lambda *_: {"result_text": "finished", "terminal_reason": "stop"}),
    )
    monkeypatch.setattr(
        drainer,
        "finish_drainer_job",
        lambda *args: completions.append(args) or True,
    )
    monkeypatch.setattr(
        drainer,
        "notify_drainer_failure",
        lambda *args: notifications.append(args),
    )
    monkeypatch.setattr(
        drainer,
        "destroy_drainer_session",
        lambda *args: destroys.append(args) or True,
    )

    result = drainer.drain_cycle.__wrapped__()
    return result, claims, starts, completions, notifications, destroys


def test_drain_cycle_claims_then_completes(monkeypatch):
    job = {
        "name": "job-1",
        "payload": {"prompt": "do work", "reasoning": True},
    }

    result, claims, starts, completions, notifications, destroys = _run(
        monkeypatch, [job]
    )

    assert result == {"status": "complete", "processed": 1}
    assert claims == [(2100, "qwen-drain"), (2100, "qwen-drain")]
    assert starts == [
        (
            "workflow-1:qwen-drain:job-1",
            "do work",
            "jomcgi-org/homelab",
            "main",
            "workflow-1",
            True,
        )
    ]
    assert completions == [("job-1", "ok", "finished")]
    assert notifications == []
    assert destroys == [(101, "workflow-1:qwen-drain:job-1")]


def test_empty_queue_exits_immediately(monkeypatch):
    result, claims, starts, completions, notifications, destroys = _run(monkeypatch, [])

    assert result == {"status": "complete", "processed": 0}
    assert claims == [(2100, "qwen-drain")]
    assert starts == []
    assert completions == []
    assert notifications == []
    assert destroys == []


def test_missing_prompt_completes_error_without_notification(monkeypatch):
    job = {"name": "bad-payload", "payload": {"repo": "weave-hand/loom"}}

    result, _, starts, completions, notifications, destroys = _run(monkeypatch, [job])

    assert result == {"status": "complete", "processed": 1}
    assert completions == [("bad-payload", "error", "missing usable prompt in payload")]
    assert starts == []
    assert notifications == []
    assert destroys == []


def test_failure_notifies_once_and_destroys_session(monkeypatch):
    job = {"name": "fails", "payload": {"prompt": "break"}}

    def fail_await(*_args):
        raise RuntimeError("turn failed")

    result, _, _, completions, notifications, destroys = _run(
        monkeypatch, [job], await_turn=fail_await
    )

    assert result == {"status": "complete", "processed": 1}
    assert completions == [("fails", "error", "turn failed")]
    assert notifications == [("fails", "turn failed")]
    assert destroys == [(101, "workflow-1:qwen-drain:fails")]


def test_start_failure_cleans_up_by_stable_session_key(monkeypatch):
    job = {"name": "start-fails", "payload": {"prompt": "run"}}

    def fail_start(*_args):
        raise RuntimeError("start failed")

    result, _, _, completions, notifications, destroys = _run(
        monkeypatch, [job], start_session=fail_start
    )

    assert result == {"status": "complete", "processed": 1}
    assert completions == [("start-fails", "error", "start failed")]
    assert notifications == [("start-fails", "start failed")]
    assert destroys == [(None, "workflow-1:qwen-drain:start-fails")]


def test_terminal_turn_error_notifies_once_and_destroys(monkeypatch):
    job = {"name": "terminal-error", "payload": {"prompt": "run"}}

    result, _, _, completions, notifications, destroys = _run(
        monkeypatch,
        [job],
        await_turn=lambda *_: {
            "result_text": "Error executing turn: transport failed",
            "terminal_reason": "error",
        },
    )

    assert result == {"status": "complete", "processed": 1}
    assert completions == [
        ("terminal-error", "error", "Error executing turn: transport failed")
    ]
    assert notifications == [
        ("terminal-error", "Error executing turn: transport failed")
    ]
    assert destroys == [(101, "workflow-1:qwen-drain:terminal-error")]


def test_timeout_completes_error_notifies_once_and_destroys(monkeypatch):
    job = {"name": "slow", "payload": {"prompt": "take your time"}}

    result, _, _, completions, notifications, destroys = _run(
        monkeypatch, [job], await_turn=lambda *_: None
    )

    assert result == {"status": "complete", "processed": 1}
    assert completions == [("slow", "error", "turn timed out after 1800 seconds")]
    assert notifications == [("slow", "turn timed out after 1800 seconds")]
    assert destroys == [(101, "workflow-1:qwen-drain:slow")]


def test_disabled_cycle_is_noop(monkeypatch):
    monkeypatch.setattr(drainer.agent_config, "drainer_enabled", lambda: False)
    monkeypatch.setattr(
        drainer,
        "pin_drainer_settings",
        lambda: (_ for _ in ()).throw(AssertionError("settings must not be read")),
    )

    assert drainer.drain_cycle.__wrapped__() == {
        "status": "disabled",
        "processed": 0,
    }


def test_session_idempotency_key_is_stable_across_step_retry(monkeypatch):
    from agent_sessions import api

    calls = []
    monkeypatch.setattr(
        api,
        "start_session_for_swarm",
        lambda *args, **kwargs: calls.append((args, kwargs)) or 7,
    )
    key = drainer._session_key("workflow-1", "job-1")

    for _ in range(2):
        assert (
            drainer.start_drainer_session.__wrapped__(
                key,
                "prompt",
                "jomcgi-org/homelab",
                "main",
                "workflow-1",
                False,
            )
            == 7
        )

    assert [call[0][0] for call in calls] == [
        "workflow-1:qwen-drain:job-1",
        "workflow-1:qwen-drain:job-1",
    ]


def test_success_summary_is_capped(monkeypatch):
    job = {"name": "verbose", "payload": {"prompt": "write"}}

    _, _, _, completions, _, _ = _run(
        monkeypatch,
        [job],
        await_turn=lambda *_: {
            "result_text": "x" * 2500,
            "terminal_reason": "stop",
        },
    )

    assert completions == [("verbose", "ok", "x" * 2000)]
