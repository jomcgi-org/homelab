from __future__ import annotations

import json

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine

from agent_sessions import provider_quota
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

_QUOTA_SPAN_ATTRIBUTES_STEP = drainer._quota_span_attributes


SETTINGS = {
    "enabled": True,
    "max_jobs_per_cycle": 3,
    "turn_timeout_seconds": 1800,
    "job_kinds": ("qwen-drain", "kg-drain"),
    "kg_max_jobs_per_day": 40,
    "repo": "jomcgi-org/homelab",
    "branch": "main",
    "reasoning": True,
    "notify_failures": True,
}


class FakeDBOS:
    workflow_id = "workflow-1"


@pytest.fixture(autouse=True)
def _clear_spans(monkeypatch):
    _EXPORTER.clear()
    monkeypatch.setattr(drainer, "sweep_kg_raws", lambda: 0)
    monkeypatch.setattr(drainer, "kg_effective_cap", lambda base_cap: base_cap)
    monkeypatch.setattr(
        drainer,
        "_quota_span_attributes",
        lambda: {},
    )
    yield


def _spans_named(name: str):
    return [s for s in _EXPORTER.get_finished_spans() if s.name == name]


def _run(
    monkeypatch,
    jobs,
    await_turn=None,
    start_session=None,
    send_message=None,
    settings=None,
):
    claims = []
    starts = []
    completions = []
    notifications = []
    destroys = []
    queued = iter([{"routine_kind": "qwen-drain", **job} for job in jobs] + [None])

    configured_settings = SETTINGS if settings is None else settings
    monkeypatch.setattr(
        drainer, "pin_drainer_settings", lambda: configured_settings.copy()
    )
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
    monkeypatch.setattr(drainer, "increment_kg_job_attempt", lambda _name: 1)
    monkeypatch.setattr(drainer, "start_agent_session", start)
    monkeypatch.setattr(
        drainer,
        "send_agent_session_message",
        send_message or (lambda *_args: 2),
    )
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


def test_kg_rejection_gets_exactly_one_correction_turn(monkeypatch):
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 0)
    monkeypatch.setattr(drainer, "build_kg_prompt", lambda _payload: "kg prompt")
    monkeypatch.setattr(
        drainer,
        "build_kg_correction_prompt",
        lambda rejected: f"correct {len(rejected)}",
    )
    applied = []

    def apply(name, payload, output, correction=False):
        applied.append((name, payload, output, correction))
        if correction:
            return {
                "atoms": ["corrected-atom"],
                "rejected": [],
                "dispute": None,
                "doc_drift": 0,
                "docfix_jobs": 0,
                "replayed": False,
            }
        return {
            "atoms": ["first-atom"],
            "rejected": [
                {"title": "bare value", "reason_code": "value", "reason": "bare"}
            ],
            "dispute": None,
            "doc_drift": 0,
            "docfix_jobs": 0,
            "replayed": False,
        }

    monkeypatch.setattr(drainer, "apply_kg_extraction", apply)
    turns = iter(
        [
            {"seq": 1, "result_text": "first", "terminal_reason": "stop"},
            {"seq": 2, "result_text": "second", "terminal_reason": "stop"},
        ]
    )
    awaits = []

    def await_turn(*args):
        awaits.append(args)
        return next(turns)

    sends = []
    job = {
        "name": "kg:raw-1",
        "routine_kind": "kg-drain",
        "payload": {"raw_id": "raw-1"},
    }

    _, _, _, completions, _, _ = _run(
        monkeypatch,
        [job],
        await_turn=await_turn,
        send_message=lambda *args: sends.append(args) or 2,
    )

    assert sends == [(101, "correct 1")]
    assert awaits == [(101, 0, 1800), (101, 1, 1800)]
    assert [call[3] for call in applied] == [False, True]
    assert completions == [
        (
            "kg:raw-1",
            "ok",
            "atoms=2 rejected=1 corrected=1 dispute=None doc_drift=0 docfix_jobs=0",
            True,
        )
    ]


@pytest.mark.parametrize(
    "first_result",
    [
        {
            "atoms": ["one"],
            "rejected": [],
            "dispute": None,
            "doc_drift": 0,
            "docfix_jobs": 0,
            "replayed": False,
        },
        {
            "atoms": ["one", "two", "three"],
            "rejected": [{"reason_code": "value"}],
            "dispute": None,
            "doc_drift": 0,
            "docfix_jobs": 0,
            "replayed": False,
        },
    ],
)
def test_kg_correction_not_sent_without_both_triggers(monkeypatch, first_result):
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 0)
    monkeypatch.setattr(drainer, "build_kg_prompt", lambda _payload: "kg prompt")
    monkeypatch.setattr(drainer, "apply_kg_extraction", lambda *_args: first_result)
    sends = []
    job = {
        "name": "kg:raw-1",
        "routine_kind": "kg-drain",
        "payload": {"raw_id": "raw-1"},
    }

    _run(monkeypatch, [job], send_message=lambda *args: sends.append(args) or 2)

    assert sends == []


def test_kg_never_sends_a_third_turn_after_rejected_correction(monkeypatch):
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 0)
    monkeypatch.setattr(drainer, "build_kg_prompt", lambda _payload: "kg prompt")
    monkeypatch.setattr(drainer, "build_kg_correction_prompt", lambda _items: "fix")
    calls = []

    def apply(_name, _payload, _output, correction=False):
        calls.append(correction)
        return {
            "atoms": [],
            "rejected": [{"reason_code": "value"}],
            "dispute": None,
            "doc_drift": 0,
            "docfix_jobs": 0,
            "replayed": False,
        }

    monkeypatch.setattr(drainer, "apply_kg_extraction", apply)
    turns = iter(
        [
            {"seq": 1, "result_text": "first", "terminal_reason": "stop"},
            {"seq": 2, "result_text": "second", "terminal_reason": "stop"},
        ]
    )
    sends = []
    job = {
        "name": "kg:raw-1",
        "routine_kind": "kg-drain",
        "payload": {"raw_id": "raw-1"},
    }

    _run(
        monkeypatch,
        [job],
        await_turn=lambda *_args: next(turns),
        send_message=lambda *args: sends.append(args) or 2,
    )

    assert calls == [False, True]
    assert sends == [(101, "fix")]


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
        drainer,
        "build_kg_prompt",
        lambda payload: prompts.append(payload) or "kg prompt",
    )
    monkeypatch.setattr(
        drainer,
        "apply_kg_extraction",
        lambda name, payload, output: (
            applied.append((name, payload, output))
            or {
                "atoms": ["one", "two"],
                "dispute": "narrowed",
                "doc_drift": 1,
                "docfix_jobs": 1,
            }
        ),
    )
    job = {
        "name": "kg:raw-1",
        "routine_kind": "kg-drain",
        "payload": {"raw_id": "raw-1"},
    }

    result, _, starts, completions, notifications, destroys = _run(monkeypatch, [job])

    assert result == {"status": "complete", "processed": 1}
    assert prompts == [{"raw_id": "raw-1"}]
    assert starts[0][0] == "workflow-1:kg-drain:kg:raw-1"
    assert starts[0][1] == "kg prompt"
    assert starts[0][6] == "kg-drain"
    assert applied == [("kg:raw-1", {"raw_id": "raw-1"}, "finished")]
    assert completions == [
        (
            "kg:raw-1",
            "ok",
            "atoms=2 rejected=0 corrected=0 dispute=narrowed doc_drift=1 docfix_jobs=1",
            True,
        )
    ]
    assert notifications == []
    assert destroys == [(101, "workflow-1:kg-drain:kg:raw-1")]


def test_kg_job_applies_full_output_beyond_summary_cap(monkeypatch):
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 0)
    monkeypatch.setattr(drainer, "build_kg_prompt", lambda _raw_id: "kg prompt")
    applied = []
    monkeypatch.setattr(
        drainer,
        "apply_kg_extraction",
        lambda name, payload, output: (
            applied.append((name, payload, output))
            or {
                "atoms": [],
                "dispute": None,
                "doc_drift": 0,
                "docfix_jobs": 0,
            }
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

    assert applied == [("kg:raw-1", {"raw_id": "raw-1"}, full_output)]


def test_repo_diff_mode_dispatches_through_repo_apply(monkeypatch):
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 0)
    prompts = []
    applied = []
    monkeypatch.setattr(
        drainer,
        "build_kg_prompt",
        lambda payload: prompts.append(payload) or "scout prompt",
    )
    monkeypatch.setattr(
        drainer,
        "apply_kg_extraction",
        lambda name, payload, output: (
            applied.append((name, payload, output)) or {"summary": "no changes"}
        ),
    )
    payload = {"mode": "repo-diff", "last_sha": None}
    job = {
        "name": "kg-repo-diff",
        "routine_kind": "kg-drain",
        "interval_secs": 3600,
        "payload": payload,
    }

    _, _, starts, completions, _, _ = _run(monkeypatch, [job])

    assert prompts == [payload]
    assert applied == [("kg-repo-diff", payload, "finished")]
    assert starts[0][1] == "scout prompt"
    assert completions == [("kg-repo-diff", "ok", "no changes", False)]


def test_retry_uses_current_repo_diff_cursor(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'retry.db'}")
    with Session(engine) as session:
        session.execute(
            text(
                """
                CREATE TABLE routine_jobs (
                    name TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )
        )
        session.execute(
            text("INSERT INTO routine_jobs (name, payload) VALUES (:name, :payload)"),
            {
                "name": "kg-repo-diff",
                "payload": json.dumps(
                    {"mode": "repo-diff", "last_sha": "b" * 40, "attempts": 1}
                ),
            },
        )
        session.commit()
    monkeypatch.setattr("core.db.get_engine", lambda: engine)

    attempt = drainer.increment_kg_job_attempt.__wrapped__("kg-repo-diff")

    with Session(engine) as session:
        payload = json.loads(
            session.execute(
                text("SELECT payload FROM routine_jobs WHERE name = 'kg-repo-diff'")
            ).scalar_one()
        )
    assert attempt == 2
    assert payload == {"mode": "repo-diff", "last_sha": "b" * 40, "attempts": 2}


def test_completed_docfix_review_is_retained_for_debounce(monkeypatch):
    job = {
        "name": "docfix-review:current",
        "routine_kind": "qwen-drain",
        "payload": {"prompt": "review docs"},
    }

    _, _, _, completions, _, _ = _run(monkeypatch, [job])

    assert completions == [("docfix-review:current", "ok", "finished")]


def test_recurring_kg_job_is_not_deregistered_on_completion(monkeypatch):
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 0)
    monkeypatch.setattr(drainer, "build_kg_prompt", lambda _payload: "kg prompt")
    monkeypatch.setattr(
        drainer,
        "apply_kg_extraction",
        lambda *_args: {
            "atoms": [],
            "dispute": None,
            "doc_drift": 0,
            "docfix_jobs": 0,
        },
    )
    job = {
        "name": "kg:recurring",
        "routine_kind": "kg-drain",
        "interval_secs": 3600,
        "payload": {"raw_id": "raw-1"},
    }

    _, _, _, completions, _, _ = _run(monkeypatch, [job])

    assert completions == [
        (
            "kg:recurring",
            "ok",
            "atoms=0 rejected=0 corrected=0 dispute=None doc_drift=0 docfix_jobs=0",
            False,
        )
    ]


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
    deferrals = []
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
    monkeypatch.setattr(drainer, "increment_kg_job_attempt", lambda _name: attempts + 1)
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
    return deferrals, completions, failures


def test_transient_kg_failure_defers_with_incremented_attempt(monkeypatch):
    deferrals, completions, failures = _run_transient_kg_failure(monkeypatch, 0)

    assert deferrals == [("kg:raw-1", 900)]
    assert completions == []
    assert failures == []


def test_transient_kg_failure_gives_up_after_retry_ceiling(monkeypatch):
    deferrals, completions, failures = _run_transient_kg_failure(monkeypatch, 2)

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


def test_failure_notifies_when_notifications_enabled_and_destroys_session(monkeypatch):
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


def test_failure_logs_without_notification_when_notifications_disabled(
    monkeypatch, caplog
):
    job = {"name": "fails-quietly", "payload": {"prompt": "break"}}

    def fail_await(*_args):
        raise RuntimeError("turn failed")

    with caplog.at_level("WARNING", logger="swarm.drainer"):
        _, _, _, _, notifications, _ = _run(
            monkeypatch,
            [job],
            await_turn=fail_await,
            settings=SETTINGS | {"notify_failures": False},
        )

    assert notifications == []
    assert "Luna drainer job fails-quietly failed: turn failed" in caplog.messages


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


def test_interrupted_turn_is_never_treated_as_completed():
    with pytest.raises(RuntimeError, match="interrupted turn cannot be completed"):
        drainer._completed_output(
            {"result_text": "preempted", "terminal_reason": "interrupted"}
        )


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


def test_job_span_includes_observed_provider_quota(monkeypatch):
    monkeypatch.setattr(
        provider_quota,
        "fetch_provider_quota_sync",
        lambda: {
            "available": True,
            "providers": {
                "codex": {
                    "observed": True,
                    "age_seconds": 42,
                    "exhausted": False,
                    "windows": [{"name": "primary", "used_percent": 24}],
                },
                "claude": {
                    "observed": True,
                    "age_seconds": 3,
                    "exhausted": True,
                    "windows": [{"name": "5h", "used_percent": 100}],
                },
            },
        },
    )
    monkeypatch.setattr(
        drainer,
        "_quota_span_attributes",
        _QUOTA_SPAN_ATTRIBUTES_STEP.__wrapped__,
    )

    result, *_rest = _run(
        monkeypatch, [{"name": "job-1", "payload": {"prompt": "work"}}]
    )

    attributes = _spans_named("drain.job")[0].attributes
    assert attributes["drain.quota.codex.used_percent"] == 24.0
    assert attributes["drain.quota.codex.age_seconds"] == 42.0
    assert attributes["drain.quota.codex.window"] == "primary"
    assert attributes["drain.quota.codex.exhausted"] is False
    assert attributes["drain.quota.claude.used_percent"] == 100.0
    assert attributes["drain.quota.claude.age_seconds"] == 3.0
    assert attributes["drain.quota.claude.window"] == "5h"
    assert attributes["drain.quota.claude.exhausted"] is True
    assert result == {"status": "complete", "processed": 1}


def test_job_span_omits_quota_when_broker_is_unavailable(monkeypatch):
    result, *_rest = _run(
        monkeypatch, [{"name": "job-1", "payload": {"prompt": "work"}}]
    )

    attributes = _spans_named("drain.job")[0].attributes
    assert not any(name.startswith("drain.quota.") for name in attributes)
    assert result == {"status": "complete", "processed": 1}


def test_quota_telemetry_exception_does_not_change_job_outcome(monkeypatch, caplog):
    def fail():
        raise RuntimeError("quota exploded")

    monkeypatch.setattr(provider_quota, "fetch_provider_quota_sync", fail)
    monkeypatch.setattr(
        drainer,
        "_quota_span_attributes",
        _QUOTA_SPAN_ATTRIBUTES_STEP.__wrapped__,
    )

    with caplog.at_level("DEBUG", logger=drainer.__name__):
        result, *_rest = _run(
            monkeypatch, [{"name": "job-1", "payload": {"prompt": "work"}}]
        )

    attributes = _spans_named("drain.job")[0].attributes
    assert not any(name.startswith("drain.quota.") for name in attributes)
    assert result == {"status": "complete", "processed": 1}
    assert "drain quota telemetry failed" in caplog.text


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


@pytest.mark.parametrize("stage", ["ordinary", "kg", "kg_correction", "recurring"])
def test_unknown_turn_holds_job_without_retry_apply_or_cleanup(monkeypatch, stage):
    held, applied, deferred = [], [], []
    monkeypatch.setattr(
        drainer, "hold_drainer_job", lambda *args: held.append(args) or True
    )
    monkeypatch.setattr(
        drainer, "defer_drainer_job", lambda *args: deferred.append(args)
    )
    monkeypatch.setattr(drainer, "kg_jobs_today", lambda: 0)
    monkeypatch.setattr(drainer, "build_kg_prompt", lambda _: "extract")
    monkeypatch.setattr(drainer, "build_kg_correction_prompt", lambda _: "correct")

    def apply(*args, **kwargs):
        applied.append(args)
        return {
            "atoms": [],
            "rejected": [{"reason": "correct"}],
            "dispute": None,
            "doc_drift": 0,
            "docfix_jobs": 0,
        }

    monkeypatch.setattr(drainer, "apply_kg_extraction", apply)
    unknown = {
        "terminal_reason": "error",
        "stop_reason": "invocation_outcome_unknown",
        "result_text": "partial extraction",
        "seq": 2,
    }
    turns = iter(
        (
            [{"terminal_reason": "stop", "result_text": "valid first result", "seq": 1}]
            if stage == "kg_correction"
            else []
        )
        + [unknown]
    )
    job = {
        "name": "job-retain",
        "routine_kind": "qwen-drain" if stage == "ordinary" else "kg-drain",
        "payload": {"prompt": "work"}
        if stage == "ordinary"
        else {"raw_id": "raw-retain"},
    }
    if stage == "recurring":
        job["interval_secs"] = 3600
    _, _, starts, completed, _, destroys = _run(
        monkeypatch, [job], await_turn=lambda *_: next(turns)
    )
    assert len(starts) == 1
    assert held == [("job-retain", 101)]
    assert deferred == []
    assert completed == []
    assert destroys == []
    assert len(applied) == (1 if stage == "kg_correction" else 0)


def test_cleanup_preserves_unknown_session_pending_and_guest(monkeypatch, tmp_path):
    from sqlmodel import SQLModel
    from agent_sessions import store
    from agent_sessions.models import AgentSession, AgentTurn, PendingMessage

    engine = create_engine(f"sqlite:///{tmp_path / 'held-cleanup.db'}")
    schemas = {table.name: table.schema for table in SQLModel.metadata.tables.values()}
    for table in SQLModel.metadata.tables.values():
        table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("core.db.get_engine", lambda: engine)
        monkeypatch.setattr("agent_sessions.mcp.get_engine", lambda: engine)
        destroyed = []

        async def destroy(guest_id):
            destroyed.append(guest_id)

        monkeypatch.setattr("agent_sessions.mcp._transport.destroy_session", destroy)
        with Session(engine) as session:
            row = store.create_session(session, "held-drainer", "<guest>", "main")
            row.ember_session_id = "guest-retain"
            row.ember_lineage_id = "lineage-retain"
            session.add(row)
            session.add(
                AgentTurn(
                    session_id=row.id,
                    seq=1,
                    prompt="original",
                    result_text="partial",
                    terminal_reason="error",
                    stop_reason="invocation_outcome_unknown",
                )
            )
            session.add(
                PendingMessage(session_id=row.id, seq=2, message_text="held next")
            )
            session.commit()
            session_id = row.id
        assert (
            drainer.destroy_drainer_session.__wrapped__(session_id, "held-drainer")
            is False
        )
        assert destroyed == []
        with Session(engine) as session:
            row = session.get(AgentSession, session_id)
            assert row.ember_session_id == "guest-retain"
            assert row.ember_lineage_id == "lineage-retain"
            assert (
                store.get_pending_message(session, session_id, 2).message_text
                == "held next"
            )
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in schemas:
                table.schema = schemas[table.name]


def test_late_drainer_completion_cannot_deregister_held_job(monkeypatch):
    from agent import routine_jobs

    deleted = []
    monkeypatch.setattr(routine_jobs, "complete_job", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        routine_jobs, "deregister_job", lambda name: deleted.append(name)
    )
    assert drainer.finish_drainer_job.__wrapped__("held", "ok", "late", True) is False
    assert deleted == []
