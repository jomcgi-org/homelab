"""The no-progress watchdog: a loop pauses and escalates, progress continues,
resume and delivery re-arm it."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory import ops_health
from factory.orchestration import factory_conductor as conductor
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_decisions as decisions
from factory.orchestration import factory_landing as landing
from factory.orchestration import factory_progress_watchdog as watchdog
from factory.orchestration import graph
from factory.orchestration.factory_intake import admit_next, receive_issue
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryReceipt,
    FactoryReviewVerdict,
    FactoryStart,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import (
    SwarmConductorCall,
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)

REPO = "owner/repo"
ISSUE = 7


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'watchdog.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = (
        SwarmTask,
        SwarmPlanVersion,
        SwarmPlanNode,
        SwarmNodeRun,
        SwarmConductorCall,
        FactoryClassTier,
        FactoryControl,
        FactoryReceipt,
        WorkItem,
        WorkItemEdge,
        WorkItemEvent,
        FactoryReviewVerdict,
        FactoryStart,
        FactoryAudit,
    )
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    for module in (conductor, controls, graph):
        monkeypatch.setattr(module, "get_engine", lambda: engine, raising=False)
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "1")
    monkeypatch.setenv("FACTORY_BACKGROUND_RESERVE", "0")
    monkeypatch.setenv("LLAMA_CPP_URL", "http://inference.test")
    yield engine
    engine.dispose()


class Github:
    def __init__(self):
        self.comments: list[dict] = []
        self.writes: list[tuple[str, str, dict]] = []

    def list(self, _repo, suffix):
        return list(self.comments) if "page=1" in suffix else []

    def get(self, _repo, suffix):
        return {"number": int(suffix.rsplit("/", 1)[1]), "state": "open", "body": "b"}

    def write(self, repo, suffix, payload, *, method="POST"):
        self.writes.append((method, suffix, payload))
        if suffix.endswith("/comments"):
            self.comments.append(
                {"body": payload["body"], "html_url": f"https://github.com/{repo}/c1"}
            )
            return self.comments[-1]
        return {}


@pytest.fixture
def github(monkeypatch):
    fake = Github()
    monkeypatch.setattr(conductor, "github_list", fake.list)
    monkeypatch.setattr(conductor, "github_get", fake.get)
    monkeypatch.setattr(landing, "github_write", fake.write)
    return fake


@pytest.fixture
def notices(monkeypatch):
    from agent import api as notify_module

    sent = []

    async def notify(text, *, level):
        sent.append((text, level))

    monkeypatch.setattr(notify_module, "notify", notify)
    return sent


class Model:
    """The chat endpoint: records each call and answers with a fixed verdict."""

    def __init__(self, verdict="progressing", reason="Each attempt narrows the bug."):
        self.calls = []
        self.content = json.dumps(
            {"verdict": verdict, "reason": reason, "evidence": ["review_1 approved"]}
        )

    def post(self, url, *, headers, json, timeout):
        self.calls.append(json)
        content = self.content

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [{"message": {"content": content}}],
                    "usage": {
                        "prompt_tokens": 4000,
                        "completion_tokens": 200,
                        "total_tokens": 4200,
                    },
                }

        return Response()


@pytest.fixture
def model(monkeypatch):
    fake = Model()
    monkeypatch.setattr(watchdog.httpx, "post", fake.post)
    return fake


class Span:
    def __init__(self):
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value


class Tracer:
    def __init__(self):
        self.spans = []

    @contextmanager
    def start_as_current_span(self, name):
        span = Span()
        self.spans.append((name, span))
        yield span


@pytest.fixture
def spans(monkeypatch):
    fake = Tracer()
    monkeypatch.setattr(watchdog, "tracer", fake)
    return fake


def policy_for(**extra):
    return {
        "repo": REPO,
        "issue_numbers": [ISSUE],
        "generation": 0,
        "max_tasks": {"delivery": 1, "advisory": 0},
        "max_turns_per_task": 20,
        "task_budget_usd": 60.0,
        "turn_budget_usd": 10.0,
        "allowed_models": ["opus", "luna"],
        "conductor_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "task_timeout_seconds": 3600,
        "max_attempts": 4,
        **extra,
    }


def admitted(**extra):
    policy = policy_for(**extra)
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    receive_issue(
        REPO,
        ISSUE,
        "Deliver the thing",
        "b",
        f"https://github.com/{REPO}/issues/{ISSUE}",
        "operator",
        task_class="bug-fix",
    )
    result = admit_next("test")
    assert result["ok"], result
    return result["task_id"], controls.task_snapshot(result["task_id"])["policy"]


_counter = Counter()


def spend(db, task_id, usd, node_key="implement_fix"):
    """One settled start that cost ``usd``."""
    _counter[task_id] += 1
    with Session(db) as session:
        session.add(
            FactoryStart(
                task_id=task_id,
                start_key=f"factory-node:{task_id}:{node_key}:{_counter[task_id]}",
                actor="test",
                model="luna",
                max_cost_usd=usd,
                status="succeeded",
                cost_usd=usd,
            )
        )
        session.commit()


def run(db, task_id, node_key, status="failed", *, head_sha=None, reason=None):
    """One settled attempt of ``node_key``, adding the node the first time."""
    with Session(db) as session:
        if not session.exec(
            select(SwarmPlanNode).where(
                SwarmPlanNode.task_id == task_id, SwarmPlanNode.node_key == node_key
            )
        ).first():
            session.add(
                SwarmPlanNode(
                    task_id=task_id,
                    node_key=node_key,
                    kind="implement",
                    prompt="p",
                    deps_json="[]",
                    max_cost_usd=10.0,
                    side_effects=True,
                    created_in_version=1,
                )
            )
        attempt = (
            len(
                session.exec(
                    select(SwarmNodeRun).where(
                        SwarmNodeRun.task_id == task_id,
                        SwarmNodeRun.node_key == node_key,
                    )
                ).all()
            )
            + 1
        )
        value = {"summary": "worked", "pr_number": None, "head_sha": head_sha}
        outcome = {"value": value}
        if reason:
            outcome["reason"] = reason
        session.add(
            SwarmNodeRun(
                task_id=task_id,
                node_key=node_key,
                attempt=attempt,
                dispatch_key=f"factory-node:{task_id}:{node_key}:{attempt}",
                pin_json=json.dumps({"model": "luna"}),
                reserved_cost_usd=10.0,
                status=status,
                cost_usd=1.0,
                head_sha=head_sha,
                outcome_json=json.dumps(outcome),
            )
        )
        session.commit()


def refuse(db, task_id, code, times):
    with controls._locked_session() as (session, _control):
        for index in range(times):
            controls._audit(
                session,
                conductor.ACTOR,
                "dispatch_refused",
                task_id=task_id,
                workflow_id=f"k{index}",
                node_key="implement_fix",
                refusal_code=code,
            )


def receipt(db, task_id):
    with Session(db) as session:
        return session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task_id)
        ).one()


def audits(db, action):
    with Session(db) as session:
        return [
            json.loads(row.detail_json)
            for row in session.exec(
                select(FactoryAudit)
                .where(FactoryAudit.action == action)
                .order_by(FactoryAudit.id)
            )
        ]


def looped(db, github, notices, spans):
    """A task past $20 with the same refusal three times: paused, card up."""
    task_id, policy = admitted()
    spend(db, task_id, 21.0)
    refuse(db, task_id, "task_turns", 3)
    assert watchdog.check(task_id, policy) == "paused"
    return task_id, policy


# ---------------------------------------------------------------------------


def test_policy_defaults_the_watchdog_so_a_live_policy_needs_no_repost():
    validated = controls.validate_policy(policy_for())
    assert validated["progress_watchdog"] == {"enabled": True, "threshold_usd": 20.0}
    assert controls.progress_watchdog_policy({}) == {
        "enabled": True,
        "threshold_usd": 20.0,
    }
    custom = controls.validate_policy(
        policy_for(progress_watchdog={"threshold_usd": 35})
    )
    assert custom["progress_watchdog"]["threshold_usd"] == 35.0
    for invalid in ({"threshold_usd": 0}, {"enabled": "yes"}, {"other": 1}):
        with pytest.raises(ValueError):
            controls.validate_policy(policy_for(progress_watchdog=invalid))
    # A stored policy from before the field compares equal to its re-post.
    stored = controls.validate_policy(policy_for())
    del stored["progress_watchdog"]
    assert controls._policy_for_generation_comparison(
        stored
    ) == controls._policy_for_generation_comparison(
        controls.validate_policy(policy_for())
    )


def test_under_threshold_does_nothing(db, github, notices, model):
    task_id, policy = admitted()
    spend(db, task_id, 19.5)
    refuse(db, task_id, "task_turns", 5)
    assert watchdog.check(task_id, policy) == "under_threshold"
    assert model.calls == []
    assert audits(db, watchdog.ASSESSED) == []


def test_loop_short_circuit_pauses_and_escalates(db, github, notices, model, spans):
    task_id, _policy = looped(db, github, notices, spans)

    assert model.calls == [], "a plain loop needs no model call"
    row = receipt(db, task_id)
    assert row.task_paused and row.state == "admitted"
    (assessed,) = audits(db, watchdog.ASSESSED)
    assert assessed["verdict"] == "looping"
    assert assessed["short_circuit"] is True
    assert "task_turns recurred 3 times" in assessed["reason"]
    pauses = [a for a in audits(db, "pause_task") if a["ok"]]
    assert len(pauses) == 1
    document = json.loads(row.escalation_json)
    assert document["kind"] == "watchdog"
    assert [o["key"] for o in document["options"]] == ["resume", "stop", "rescope"]
    assert controls.verify_option_list(document["options"], subject="watchdog") is None
    assert "refusal_code=task_turns x3" in document["evidence"]
    assert f"factory-watchdog:{task_id}:1" in github.comments[0]["body"]
    assert "no progress" in github.comments[0]["body"]
    assert len(notices) == 1 and "watchdog paused" in notices[0][0]
    ((name, span),) = spans.spans
    assert name == "factory.watchdog.assess"
    assert span.attributes["factory.task_id"] == task_id
    assert span.attributes["factory.watchdog.spend_usd"] == 21.0
    assert span.attributes["factory.watchdog.threshold_usd"] == 20.0
    assert span.attributes["factory.watchdog.verdict"] == "looping"
    assert span.attributes["factory.watchdog.short_circuit"] is True
    # The reconciler's own two-hour pause expiry never cancels this pause.
    assert conductor._expire_reconciler_pause(task_id) is False
    # And the next tick waits for a person rather than asking again.
    assert watchdog.check(task_id, _policy) == "awaiting_person"
    assert len(audits(db, watchdog.ASSESSED)) == 1


def test_progressing_verdict_continues_and_rearms_next_step(
    db, github, notices, model, spans
):
    task_id, policy = admitted()
    spend(db, task_id, 21.0)
    run(db, task_id, "investigate_bug", "succeeded")

    assert watchdog.check(task_id, policy) == "progressing"

    assert len(model.calls) == 1
    prompt = json.loads(model.calls[0]["messages"][1]["content"])
    assert prompt["node_history"][0]["node_key"] == "investigate_bug"
    assert model.calls[0]["max_tokens"] == watchdog.MODEL_MAX_TOKENS
    assert not receipt(db, task_id).task_paused
    assert receipt(db, task_id).escalation_json is None
    (assessed,) = audits(db, watchdog.ASSESSED)
    assert assessed["verdict"] == "progressing"
    assert assessed["short_circuit"] is False
    assert assessed["next_threshold_usd"] == 40.0
    assert 0 < assessed["cost_usd"] < 0.01
    assert spans.spans[0][1].attributes["factory.watchdog.short_circuit"] is False
    # Once per crossing: more spend under the next step asks nothing.
    spend(db, task_id, 10.0)
    assert watchdog.check(task_id, policy) == "under_threshold"
    assert len(model.calls) == 1
    # The assessment is visible on the task detail, cost included.
    with Session(db) as session:
        shown = watchdog.detail(session, task_id, policy)
    assert shown["last"]["verdict"] == "progressing"
    assert shown["next_threshold_usd"] == 40.0
    assert shown["assessment_cost_usd"] == assessed["cost_usd"]
    assert shown["paused_by_watchdog"] is False


def test_unreadable_assessment_fails_closed_to_a_pause(
    db, github, notices, model, spans
):
    model.content = "I think it is fine"
    task_id, policy = admitted()
    spend(db, task_id, 25.0)
    assert watchdog.check(task_id, policy) == "paused"
    (assessed,) = audits(db, watchdog.ASSESSED)
    assert assessed["reason"].startswith("assessment_unavailable")


def test_resume_from_the_card_rearms_from_current_spend(
    db, github, notices, model, spans
):
    task_id, policy = looped(db, github, notices, spans)
    row = receipt(db, task_id)

    decided = decisions.apply_decision(row.id, "resume", "operator")
    assert decided["ok"] and decided["applied"]
    spend(db, task_id, 3.0)  # an attempt that was in flight at the pause settles
    assert watchdog.check(task_id, policy) == "cleared"

    assert not receipt(db, task_id).task_paused
    (cleared,) = audits(db, watchdog.CLEARED)
    assert cleared["reason"] == "resume"
    assert cleared["spend_usd"] == 24.0
    assert cleared["next_threshold_usd"] == 44.0
    # Re-armed: the refusals before the resume no longer count, and spend up
    # to the next step is left alone.
    spend(db, task_id, 15.0)
    assert watchdog.check(task_id, policy) == "under_threshold"
    spend(db, task_id, 6.0)
    assert watchdog.check(task_id, policy) == "progressing"
    assert len(model.calls) == 1


def test_plain_resume_task_also_rearms_and_answers_the_card(
    db, github, notices, model, spans
):
    task_id, policy = looped(db, github, notices, spans)
    assert controls.set_control("resume_task", "operator", task_id=task_id)["ok"]

    assert watchdog.check(task_id, policy) == "cleared"
    (cleared,) = audits(db, watchdog.CLEARED)
    assert cleared["resumed_by"] == "operator"
    document = json.loads(receipt(db, task_id).escalation_json)
    assert document["resolved"]["option_key"] == "resume"


def _bump_generation():
    # Configure accepts a new generation while work runs; the active task
    # keeps its pinned generation-0 policy.
    assert controls.set_control(
        "configure", "operator", policy=policy_for(generation=1)
    )["ok"]
    assert controls.status()["policy"]["generation"] == 1


def test_a_card_on_an_active_old_generation_task_is_decidable(
    db, github, notices, model, spans, monkeypatch
):
    # 2026-10-01: receipts 700 and 703 were still admitted on generation 13
    # when the policy moved to 14, and factory_decide refused their watchdog
    # cards as stale, so the only way out was resume_task by hand.
    monkeypatch.setenv("FACTORY_ACTIVE_RECEIPT_DECISIONS_ENABLED", "true")
    task_id, policy = looped(db, github, notices, spans)
    _bump_generation()
    row = receipt(db, task_id)
    assert row.generation == 0 and row.state == "admitted"

    result = decisions.request_decision(
        row.id,
        decisions._fields(row)["decision_id"],
        "resume",
        "operator",
        request_key="active-old-generation",
    )
    assert result["ok"], result
    assert watchdog.check(task_id, policy) == "cleared"
    assert not receipt(db, task_id).task_paused


def test_without_the_flag_an_active_old_generation_card_is_refused(
    db, github, notices, model, spans, monkeypatch
):
    monkeypatch.delenv("FACTORY_ACTIVE_RECEIPT_DECISIONS_ENABLED", raising=False)
    task_id, _policy = looped(db, github, notices, spans)
    _bump_generation()
    row = receipt(db, task_id)
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.apply_decision(row.id, "resume", "operator")
    assert "receipt generation 0" in raised.value.reason


def test_a_settled_old_generation_card_is_still_refused_with_the_flag(
    db, github, notices, model, spans, monkeypatch
):
    monkeypatch.setenv("FACTORY_ACTIVE_RECEIPT_DECISIONS_ENABLED", "true")
    task_id, _policy = looped(db, github, notices, spans)
    with Session(db) as session:
        stored = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task_id)
        ).one()
        stored.state = "escalated"
        session.add(stored)
        session.commit()
    _bump_generation()
    row = receipt(db, task_id)
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.apply_decision(row.id, "resume", "operator")
    assert "receipt generation 0" in raised.value.reason


def test_stop_from_the_card_cancels_the_task(db, github, notices, model, spans):
    task_id, policy = looped(db, github, notices, spans)
    row = receipt(db, task_id)

    assert decisions.apply_decision(row.id, "stop", "operator")["ok"]
    assert watchdog.check(task_id, policy) == "stopped"
    assert receipt(db, task_id).state == "cancelled"
    assert watchdog.check(task_id, policy) == "not_watched"


def test_delivery_clears_the_watchdog_without_an_assessment(
    db, github, notices, model, spans
):
    task_id, policy = admitted()
    spend(db, task_id, 22.0)
    refuse(db, task_id, "task_turns", 3)
    run(db, task_id, "implement_fix", "succeeded", head_sha="a" * 40)

    assert watchdog.check(task_id, policy) == "cleared"

    assert model.calls == [] and spans.spans == []
    (cleared,) = audits(db, watchdog.CLEARED)
    assert cleared["reason"] == "delivery"
    assert cleared["delivered"] == ["sha:" + "a" * 40]
    assert cleared["next_threshold_usd"] == 42.0
    assert not receipt(db, task_id).task_paused
    # The same commit reported again is not a second delivery.
    run(db, task_id, "review_1", "succeeded", head_sha="a" * 40)
    spend(db, task_id, 21.0)
    assert watchdog.check(task_id, policy) == "progressing"


def test_an_attempt_in_flight_is_waited_for_within_one_step(
    db, github, notices, model, spans
):
    task_id, policy = admitted()
    spend(db, task_id, 21.0)
    run(db, task_id, "implement_fix", "dispatched")
    assert watchdog.check(task_id, policy) == "waiting_for_attempt"
    spend(db, task_id, 20.0)
    assert watchdog.check(task_id, policy) == "progressing"


def test_disabled_policy_never_assesses(db, github, notices, model, spans):
    task_id, policy = admitted(progress_watchdog={"enabled": False})
    spend(db, task_id, 50.0)
    assert watchdog.check(task_id, policy) == "disabled"


def test_health_reports_watchdog_paused_tasks(db, github, notices, model, spans):
    task_id, _policy = looped(db, github, notices, spans)
    with Session(db) as session:
        held = watchdog.paused_receipts(session)
    assert held == [(receipt(db, task_id).id, ISSUE, 21.0)]
    result = ops_health.evaluate_factory(None, [], [], ops_health._now(), held)
    assert result["ok"] is False
    assert "1 task(s) paused by the no-progress watchdog" in result["detail"]
    # Resumed, it is no longer held.
    assert controls.set_control("resume_task", "operator", task_id=task_id)["ok"]
    with Session(db) as session:
        assert watchdog.paused_receipts(session) == []


def _runs(*specs):
    return [
        {
            "id": index,
            "node_key": key,
            "attempt": 1,
            "status": status,
            "head_sha": None,
            "base_sha": None,
            "outcome_json": json.dumps({"reason": reason} if reason else {}),
        }
        for index, (key, status, reason) in enumerate(specs, start=1)
    ]


def test_deterministic_loop_shapes():
    empty = {"refusals": [], "adds": Counter(), "active": []}
    readded = {
        **empty,
        "runs": _runs(("fix_it", "failed", "a"), ("fix_it", "failed", "b")),
        "adds": Counter({"fix_it": 2}),
    }
    assert "re-added 2 times" in watchdog.deterministic_loop(readded)[0]
    identical = {
        **empty,
        "runs": _runs(*[(f"investigate_{i}", "failed", "tests red") for i in range(3)]),
    }
    assert "failed identically" in watchdog.deterministic_loop(identical)[0]
    no_commit = {
        **empty,
        "runs": _runs(
            ("implement_a", "succeeded", None),
            ("correct_1", "failed", None),
            ("correct_2", "succeeded", None),
        ),
    }
    assert "no new commit" in watchdog.deterministic_loop(no_commit)[0]
    two_refusals = {**empty, "runs": [], "refusals": ["x", "x", "y"]}
    assert watchdog.deterministic_loop(two_refusals) is None
