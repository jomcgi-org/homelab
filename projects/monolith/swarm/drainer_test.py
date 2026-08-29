from __future__ import annotations

import pytest

import swarm.drainer as drainer


SETTINGS = {
    "enabled": True,
    "max_jobs_per_cycle": 3,
    "turn_timeout_seconds": 1800,
    "job_kind": "qwen-drain",
    "repo": "jomcgi-org/homelab",
    "branch": "main",
    "reasoning": True,
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
    monkeypatch.setattr(drainer, "start_agent_session", start)
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
            "qwen",
            "jomcgi-org/homelab",
            "main",
            "workflow-1",
            "qwen-drain",
            None,
            True,
        )
    ]
    assert completions == [("job-1", "ok", "finished")]
    assert notifications == []
    assert destroys == [(101, "workflow-1:qwen-drain:job-1")]


@pytest.mark.parametrize(
    ("payload_reasoning", "lane_default", "expected"),
    [
        # No key in the payload takes the lane default, which is what every
        # registered job does today. Before this defaulted from settings the
        # answer was always False, and thinking off is what makes qwen loop.
        (None, True, True),
        (None, False, False),
        # An explicit per-job value still wins over the lane default in both
        # directions, so a job can opt out of a slow thinking run.
        (False, True, False),
        (True, False, True),
    ],
)
def test_reasoning_defaults_from_settings_and_payload_overrides(
    monkeypatch, payload_reasoning, lane_default, expected
):
    payload = {"prompt": "do work"}
    if payload_reasoning is not None:
        payload["reasoning"] = payload_reasoning

    settings = SETTINGS | {"reasoning": lane_default}
    monkeypatch.setattr(drainer, "pin_drainer_settings", lambda: settings.copy())

    _prompt, _repo, _branch, reasoning = drainer._payload_values(payload, settings)

    assert reasoning is expected


def test_reasoning_survives_replayed_settings_without_the_key():
    """A cycle recovered across this deploy replays settings pinned before it.

    pin_drainer_settings is a checkpointed step, so the dict a recovered cycle
    sees is the one written by the previous image, which has no "reasoning"
    key. An eager subscript would raise KeyError, the per-job handler would
    finish each claimed job as "error", and for a one-shot job that is
    permanent. repo and branch never needed this because they predate pinning.
    """
    legacy_settings = {
        "enabled": True,
        "max_jobs_per_cycle": 3,
        "turn_timeout_seconds": 1800,
        "job_kind": "qwen-drain",
        "repo": "jomcgi-org/homelab",
        "branch": "main",
    }

    _prompt, _repo, _branch, reasoning = drainer._payload_values(
        {"prompt": "do work"}, legacy_settings
    )
    assert reasoning is False

    _p, _r, _b, explicit = drainer._payload_values(
        {"prompt": "do work", "reasoning": True}, legacy_settings
    )
    assert explicit is True


def test_empty_queue_exits_immediately(monkeypatch):
    result, claims, starts, completions, notifications, destroys = _run(monkeypatch, [])

    assert result == {"status": "complete", "processed": 0}
    assert claims == [(2100, "qwen-drain")]
    assert starts == []
    assert completions == []
    assert notifications == []
    assert destroys == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "missing usable prompt in payload"),
        ({"repo": "weave-hand/loom"}, "missing usable prompt in payload"),
        ({"prompt": "work", "repo": ""}, "repo must be a non-empty string"),
        ({"prompt": "work", "branch": 7}, "branch must be a non-empty string"),
        ({"prompt": "work", "reasoning": "yes"}, "reasoning must be a boolean"),
    ],
)
def test_malformed_payload_completes_error_without_notification(
    monkeypatch, payload, message
):
    job = {"name": "bad-payload", "payload": payload}

    result, _, starts, completions, notifications, destroys = _run(monkeypatch, [job])

    assert result == {"status": "complete", "processed": 1}
    assert completions == [("bad-payload", "error", message)]
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
    monkeypatch.setattr(
        drainer.agent_config,
        "drainer_enabled",
        lambda: (_ for _ in ()).throw(AssertionError("env must not be re-read")),
    )
    settings = SETTINGS.copy()
    settings["enabled"] = False
    monkeypatch.setattr(drainer, "pin_drainer_settings", lambda: settings)

    assert drainer.drain_cycle.__wrapped__() == {
        "status": "disabled",
        "processed": 0,
    }


def test_session_idempotency_key_is_stable():
    assert drainer._session_key("workflow-1", "job-1") == (
        "workflow-1:qwen-drain:job-1"
    )


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


def test_full_batch_chains_the_next_cycle(monkeypatch):
    """Hitting max_jobs_per_cycle means work remains, so chain immediately.

    Without this a deep backlog drains in bursts, idling at every cycle
    boundary until the next */15 tick.
    """
    chained = []
    monkeypatch.setattr(drainer, "chain_next_cycle", lambda: chained.append(True))

    jobs = [
        {"name": f"job-{i}", "payload": {"prompt": "work"}}
        for i in range(SETTINGS["max_jobs_per_cycle"])
    ]
    result, _claims, _starts, _completions, _notifications, _destroys = _run(
        monkeypatch, jobs
    )

    assert result == {
        "status": "complete",
        "processed": SETTINGS["max_jobs_per_cycle"],
    }
    assert chained == [True]


def test_partial_batch_does_not_chain(monkeypatch):
    """An empty queue must cost nothing, which is what stops this spinning."""
    chained = []
    monkeypatch.setattr(drainer, "chain_next_cycle", lambda: chained.append(True))

    jobs = [{"name": "job-1", "payload": {"prompt": "work"}}]
    result, _claims, _starts, _completions, _notifications, _destroys = _run(
        monkeypatch, jobs
    )

    assert result == {"status": "complete", "processed": 1}
    assert chained == []


def test_empty_queue_does_not_chain(monkeypatch):
    chained = []
    monkeypatch.setattr(drainer, "chain_next_cycle", lambda: chained.append(True))

    result, _claims, _starts, _completions, _notifications, _destroys = _run(
        monkeypatch, []
    )

    assert result == {"status": "complete", "processed": 0}
    assert chained == []


def test_failed_jobs_still_count_toward_chaining(monkeypatch):
    """A full batch of FAILING jobs still means the backlog was deep.

    processed counts claims, not successes, on purpose: a run of jobs that all
    error is still evidence there was more work than one cycle could take, and
    refusing to chain there would stall the queue exactly when it is longest.
    """
    chained = []
    monkeypatch.setattr(drainer, "chain_next_cycle", lambda: chained.append(True))

    def fail_turn(*_args):
        raise RuntimeError("turn failed")

    jobs = [
        {"name": f"job-{i}", "payload": {"prompt": "work"}}
        for i in range(SETTINGS["max_jobs_per_cycle"])
    ]
    result, _claims, _starts, _completions, _notifications, _destroys = _run(
        monkeypatch, jobs, await_turn=fail_turn
    )

    assert result["processed"] == SETTINGS["max_jobs_per_cycle"]
    assert chained == [True]
