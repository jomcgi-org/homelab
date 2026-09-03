from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
import pytest

import swarm.drainer as drainer

# trace.get_tracer returns a ProxyTracer that resolves the provider lazily, at
# the first span rather than at import, so installing this after importing the
# module under test is what makes the spans land here. The provider is global
# and can only be set once, which is fine: every py_test target is its own
# process.
_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


SETTINGS = {
    "enabled": True,
    "max_jobs_per_cycle": 3,
    "turn_timeout_seconds": 1800,
    "job_kinds": ("qwen-drain", "kg-drain"),
    "kg_max_jobs_per_day": 40,
    "repo": "jomcgi-org/homelab",
    "branch": "main",
    "reasoning": True,
}


class FakeDBOS:
    workflow_id = "workflow-1"


@pytest.fixture(autouse=True)
def _clear_spans(monkeypatch):
    _EXPORTER.clear()
    monkeypatch.setattr(drainer, "sweep_kg_raws", lambda: 0)
    yield


def _spans_named(name: str):
    return [s for s in _EXPORTER.get_finished_spans() if s.name == name]


def _run(monkeypatch, jobs, await_turn=None, start_session=None):
    claims = []
    starts = []
    completions = []
    notifications = []
    destroys = []
    queued = iter([{"routine_kind": "qwen-drain", **job} for job in jobs] + [None])

    monkeypatch.setattr(drainer, "pin_drainer_settings", lambda: SETTINGS.copy())
    monkeypatch.setattr(drainer, "DBOS", FakeDBOS)

    def claim(ttl_secs, kinds):
        claims.append((ttl_secs, kinds))
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
    assert claims == [
        (2100, ("qwen-drain", "kg-drain")),
        (2100, ("qwen-drain", "kg-drain")),
    ]
    assert starts == [
        (
            "workflow-1:qwen-drain:job-1",
            "do work",
            "luna",
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


def test_kg_job_builds_prompt_uses_kg_session_and_applies(monkeypatch):
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 2)
    prompts = []
    applied = []
    monkeypatch.setattr(
        drainer, "build_kg_prompt", lambda raw_id: prompts.append(raw_id) or "kg prompt"
    )
    monkeypatch.setattr(
        drainer,
        "apply_kg_extraction",
        lambda raw_id, output: (
            applied.append((raw_id, output))
            or {"atoms": ["one", "two"], "dispute": "narrowed"}
        ),
    )
    job = {
        "name": "kg:raw-1",
        "routine_kind": "kg-drain",
        "payload": {"raw_id": "raw-1"},
    }

    result, _, starts, completions, notifications, destroys = _run(monkeypatch, [job])

    assert result == {"status": "complete", "processed": 1}
    assert prompts == ["raw-1"]
    assert starts[0][0] == "workflow-1:kg-drain:kg:raw-1"
    assert starts[0][1] == "kg prompt"
    assert starts[0][6] == "kg-drain"
    assert applied == [("raw-1", "finished")]
    assert completions == [("kg:raw-1", "ok", "atoms=2 dispute=narrowed", True)]
    assert notifications == []
    assert destroys == [(101, "workflow-1:kg-drain:kg:raw-1")]


def test_kg_job_applies_full_output_beyond_summary_cap(monkeypatch):
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 0)
    monkeypatch.setattr(drainer, "build_kg_prompt", lambda _raw_id: "kg prompt")
    applied = []
    monkeypatch.setattr(
        drainer,
        "apply_kg_extraction",
        lambda raw_id, output: (
            applied.append((raw_id, output)) or {"atoms": [], "dispute": None}
        ),
    )
    full_output = "x" * (drainer.SUMMARY_MAX_CHARS + 1)
    job = {
        "name": "kg:raw-1",
        "routine_kind": "kg-drain",
        "payload": {"raw_id": "raw-1"},
    }

    _run(
        monkeypatch,
        [job],
        await_turn=lambda *_: {
            "result_text": full_output,
            "terminal_reason": "stop",
        },
    )

    assert applied == [("raw-1", full_output)]


def test_kg_daily_cap_defers_without_processing_or_notification(monkeypatch):
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 40)
    deferred = []
    monkeypatch.setattr(
        drainer,
        "defer_drainer_job",
        lambda name, seconds: deferred.append((name, seconds)) or True,
    )
    job = {
        "name": "kg:raw-1",
        "routine_kind": "kg-drain",
        "payload": {"raw_id": "raw-1"},
    }

    result, _, starts, completions, notifications, destroys = _run(monkeypatch, [job])

    assert result == {"status": "complete", "processed": 0}
    assert completions == [("kg:raw-1", "deferred", "kg daily cap reached")]
    assert deferred == [("kg:raw-1", 3600)]
    assert starts == []
    assert notifications == []
    assert destroys == []


def test_invalid_kg_output_defers_on_bounded_retry_path(monkeypatch):
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 0)
    monkeypatch.setattr(drainer, "build_kg_prompt", lambda _raw_id: "kg prompt")

    def invalid(*_args):
        raise drainer.ExtractionOutputInvalid("invalid extraction")

    monkeypatch.setattr(drainer, "apply_kg_extraction", invalid)
    payload_updates = []
    deferrals = []
    monkeypatch.setattr(
        drainer,
        "update_drainer_job_payload",
        lambda name, payload: payload_updates.append((name, payload)) or True,
    )
    monkeypatch.setattr(
        drainer,
        "defer_drainer_job",
        lambda name, seconds: deferrals.append((name, seconds)) or True,
    )
    job = {
        "name": "kg:raw-1",
        "routine_kind": "kg-drain",
        "payload": {"raw_id": "raw-1"},
    }

    _, _, _, completions, notifications, destroys = _run(monkeypatch, [job])

    assert payload_updates == [("kg:raw-1", {"raw_id": "raw-1", "attempts": 1})]
    assert deferrals == [("kg:raw-1", 900)]
    assert completions == []
    assert notifications == [("kg:raw-1", "invalid extraction")]
    assert destroys == [(101, "workflow-1:kg-drain:kg:raw-1")]


@pytest.mark.parametrize(
    ("payload_reasoning", "lane_default", "expected"),
    [
        # No key in the payload takes the lane default, which is what every
        # registered job does today. Before this defaulted from settings the
        # answer was always False, so the configured Luna lane default was lost.
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
        "job_kinds": ("qwen-drain",),
        "kg_max_jobs_per_day": 40,
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
    assert claims == [(2100, ("qwen-drain", "kg-drain"))]
    assert starts == []
    assert completions == []
    assert notifications == []
    assert destroys == []


def test_kg_sweep_runs_at_cycle_start_and_updates_health_value(monkeypatch):
    health_updates = []
    monkeypatch.setattr(drainer, "sweep_kg_raws", lambda: 4)
    monkeypatch.setattr(
        drainer,
        "set_kg_swept_last_cycle",
        lambda count: health_updates.append(count),
    )

    result, *_ = _run(monkeypatch, [])

    assert result == {"status": "complete", "processed": 0}
    assert health_updates == [4]


def test_empty_job_kinds_pause_claims(monkeypatch):
    claims = []
    monkeypatch.setattr(
        drainer,
        "pin_drainer_settings",
        lambda: SETTINGS | {"job_kinds": ()},
    )
    monkeypatch.setattr(drainer, "DBOS", FakeDBOS)
    monkeypatch.setattr(
        drainer,
        "claim_drainer_job",
        lambda *_args: claims.append(True),
    )

    assert drainer.drain_cycle.__wrapped__() == {
        "status": "complete",
        "processed": 0,
    }
    assert claims == []


def test_kg_cap_defers_once_then_drains_qwen_jobs(monkeypatch):
    settings = SETTINGS | {"max_jobs_per_cycle": 15, "kg_max_jobs_per_day": 40}
    queue = [
        {
            "name": f"kg:raw-{index}",
            "routine_kind": "kg-drain",
            "payload": {"raw_id": f"raw-{index}"},
        }
        for index in range(20)
    ] + [
        {
            "name": f"qwen-{index}",
            "routine_kind": "qwen-drain",
            "payload": {"prompt": "work"},
        }
        for index in range(2)
    ]
    claims = []
    deferred = []
    completions = []
    starts = []

    def claim(_ttl, kinds):
        claims.append(tuple(kinds))
        return next((job for job in queue if job["routine_kind"] in kinds), None)

    def remove_claimed(*args):
        starts.append(args)
        name = args[0].split(":qwen-drain:", 1)[1]
        queue[:] = [job for job in queue if job["name"] != name]
        return len(starts)

    monkeypatch.setattr(drainer, "pin_drainer_settings", lambda: settings)
    monkeypatch.setattr(drainer, "DBOS", FakeDBOS)
    monkeypatch.setattr(drainer, "claim_drainer_job", claim)
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 40)
    monkeypatch.setattr(
        drainer,
        "finish_drainer_job",
        lambda *args: completions.append(args) or True,
    )
    monkeypatch.setattr(
        drainer,
        "defer_drainer_job",
        lambda name, seconds: (
            (
                deferred.append((name, seconds)),
                queue.remove(next(job for job in queue if job["name"] == name)),
            )
            and True
        ),
    )
    monkeypatch.setattr(drainer, "start_agent_session", remove_claimed)
    monkeypatch.setattr(
        drainer,
        "_await_turn",
        lambda *_args: {"result_text": "done", "terminal_reason": "stop"},
    )
    monkeypatch.setattr(drainer, "destroy_drainer_session", lambda *_args: True)
    monkeypatch.setattr(drainer, "chain_next_cycle", lambda: None)

    result = drainer.drain_cycle.__wrapped__()

    assert result == {"status": "complete", "processed": 2}
    assert deferred == [("kg:raw-0", 3600)]
    assert [args[0] for args in starts] == [
        "workflow-1:qwen-drain:qwen-0",
        "workflow-1:qwen-drain:qwen-1",
    ]
    assert claims[0] == ("qwen-drain", "kg-drain")
    assert all(kinds == ("qwen-drain",) for kinds in claims[1:])


def _run_transient_kg_failure(monkeypatch, attempts):
    job = {
        "name": "kg:raw-1",
        "routine_kind": "kg-drain",
        "payload": {"raw_id": "raw-1", "attempts": attempts},
    }
    queue = iter([job, None])
    payload_updates = []
    deferrals = []
    completions = []
    failures = []
    monkeypatch.setattr(drainer, "pin_drainer_settings", lambda: SETTINGS.copy())
    monkeypatch.setattr(drainer, "DBOS", FakeDBOS)
    monkeypatch.setattr(drainer, "claim_drainer_job", lambda *_args: next(queue))
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 0)
    monkeypatch.setattr(drainer, "build_kg_prompt", lambda _raw_id: "kg prompt")
    monkeypatch.setattr(drainer, "start_agent_session", lambda *_args: 101)
    monkeypatch.setattr(
        drainer,
        "_await_turn",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("ember unavailable")),
    )
    monkeypatch.setattr(
        drainer,
        "update_drainer_job_payload",
        lambda name, payload: payload_updates.append((name, payload)) or True,
    )
    monkeypatch.setattr(
        drainer,
        "defer_drainer_job",
        lambda name, seconds: deferrals.append((name, seconds)) or True,
    )
    monkeypatch.setattr(
        drainer,
        "finish_drainer_job",
        lambda *args: completions.append(args) or True,
    )
    monkeypatch.setattr(
        drainer,
        "record_kg_failure",
        lambda raw_id, error, attempt: failures.append((raw_id, error, attempt)),
    )
    monkeypatch.setattr(drainer, "notify_drainer_failure", lambda *_args: None)
    monkeypatch.setattr(drainer, "destroy_drainer_session", lambda *_args: True)

    drainer.drain_cycle.__wrapped__()
    return payload_updates, deferrals, completions, failures


def test_transient_kg_failure_defers_with_incremented_attempt(monkeypatch):
    payload_updates, deferrals, completions, failures = _run_transient_kg_failure(
        monkeypatch, 0
    )

    assert payload_updates == [("kg:raw-1", {"raw_id": "raw-1", "attempts": 1})]
    assert deferrals == [("kg:raw-1", 900)]
    assert completions == []
    assert failures == []


def test_transient_kg_failure_gives_up_after_retry_ceiling(monkeypatch):
    payload_updates, deferrals, completions, failures = _run_transient_kg_failure(
        monkeypatch, 2
    )

    assert payload_updates == [("kg:raw-1", {"raw_id": "raw-1", "attempts": 3})]
    assert deferrals == []
    assert completions == [("kg:raw-1", "error", "ember unavailable", True)]
    assert failures == [("raw-1", "ember unavailable", 3)]


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


def test_fully_failed_batch_does_not_chain(monkeypatch):
    """An all-failing batch must fall back to tick pace, not accelerate.

    When the downstream is sick every claimed job fails in seconds, and a
    failed one-shot is permanently done because complete_job NULLs its
    next_run_at whatever the status. Chaining through that destroys the
    backlog at hundreds of dead jobs an hour. Falling back to the next tick
    is a 15 minute backoff exactly when something is wrong.
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
    assert chained == [], "a batch with zero successes must not chain"


def test_zero_max_jobs_per_cycle_does_not_chain(monkeypatch):
    """A bound of zero must not chain.

    DRAINER_MAX_JOBS_PER_CYCLE is unvalidated int(env), so setting it to 0 as
    a way to pause the lane is plausible. Without the processed guard, 0 >= 0
    holds and every cycle chains an endless one-per-second no-op.
    """
    chained = []
    monkeypatch.setattr(drainer, "chain_next_cycle", lambda: chained.append(True))
    settings = SETTINGS | {"max_jobs_per_cycle": 0}
    monkeypatch.setattr(drainer, "pin_drainer_settings", lambda: settings.copy())

    result, _claims, _starts, _completions, _notifications, _destroys = _run(
        monkeypatch, []
    )

    assert result == {"status": "complete", "processed": 0}
    assert chained == []


def test_drain_cycle_emits_a_cycle_span(monkeypatch):
    job = {"name": "job-1", "payload": {"prompt": "work"}}

    _run(monkeypatch, [job])

    spans = _spans_named("drain.cycle")
    assert len(spans) == 1
    assert spans[0].attributes["drain.outcome"] == "queue_empty"
    assert spans[0].attributes["drain.jobs_claimed"] == 1
    assert spans[0].attributes["drain.jobs_succeeded"] == 1
    assert spans[0].attributes["drain.chained"] is False
    assert spans[0].attributes["drain.workflow_id"] == "workflow-1"


def test_each_claimed_job_emits_a_job_span(monkeypatch):
    monkeypatch.setattr(drainer, "chain_next_cycle", lambda: None)
    jobs = [
        {"name": f"job-{i}", "payload": {"prompt": "work"}}
        for i in range(SETTINGS["max_jobs_per_cycle"])
    ]

    _run(monkeypatch, jobs)

    spans = _spans_named("drain.job")
    assert len(spans) == SETTINGS["max_jobs_per_cycle"]
    assert [span.attributes["drain.job_name"] for span in spans] == [
        job["name"] for job in jobs
    ]


def test_a_failing_job_still_ends_its_spans(monkeypatch):
    job = {"name": "fails", "payload": {"prompt": "break"}}

    def fail_await(*_args):
        raise RuntimeError("turn failed")

    _run(monkeypatch, [job], await_turn=fail_await)

    job_spans = _spans_named("drain.job")
    cycle_spans = _spans_named("drain.cycle")
    assert len(job_spans) == 1
    assert len(cycle_spans) == 1
    assert "drain.outcome" not in job_spans[0].attributes
    assert "drain.status" not in job_spans[0].attributes


def test_full_batch_cycle_span_records_the_chain(monkeypatch):
    monkeypatch.setattr(drainer, "chain_next_cycle", lambda: None)
    jobs = [
        {"name": f"job-{i}", "payload": {"prompt": "work"}}
        for i in range(SETTINGS["max_jobs_per_cycle"])
    ]

    _run(monkeypatch, jobs)

    spans = _spans_named("drain.cycle")
    assert len(spans) == 1
    assert spans[0].attributes["drain.outcome"] == "bound_reached"
    assert spans[0].attributes["drain.chained"] is True


def test_disabled_cycle_still_emits_a_cycle_span(monkeypatch):
    settings = SETTINGS | {"enabled": False}
    monkeypatch.setattr(drainer, "pin_drainer_settings", lambda: settings)

    drainer.drain_cycle.__wrapped__()

    spans = _spans_named("drain.cycle")
    assert len(spans) == 1
    assert spans[0].attributes["drain.outcome"] == "disabled"


def test_claim_step_span_lives_inside_the_step_body(monkeypatch):
    import agent.routine_jobs as routine_jobs

    monkeypatch.setattr(
        routine_jobs,
        "claim_job",
        lambda **_kwargs: {"name": "job-1", "payload": {"prompt": "work"}},
    )

    drainer.claim_drainer_job.__wrapped__(60, ["qwen-drain", "kg-drain"])

    spans = _spans_named("drain.claim_job")
    assert len(spans) == 1
    assert spans[0].attributes["drain.claimed"] is True
    assert spans[0].attributes["drain.job_name"] == "job-1"

    _EXPORTER.clear()
    # A replayed step emits nothing because its body does not execute again.
    assert _spans_named("drain.claim_job") == []


def test_finish_step_span_marks_error_status(monkeypatch):
    import agent.routine_jobs as routine_jobs

    monkeypatch.setattr(routine_jobs, "complete_job", lambda *_args, **_kwargs: True)

    drainer.finish_drainer_job.__wrapped__("job-1", "error", "boom")

    spans = _spans_named("drain.finish_job")
    assert len(spans) == 1
    assert spans[0].attributes["drain.status"] == "error"
    assert spans[0].status.status_code is StatusCode.ERROR
