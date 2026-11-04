"""Routine escalations default to their recommendation; recurring ones become repairs."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_auto_recommendation as auto
from factory.orchestration import factory_conductor as conductor
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_decisions as decisions
from factory.orchestration import factory_landing as landing
from factory.orchestration.factory_intake import receive_issue
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
PR = 70


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auto.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = (
        AgentSession,
        PendingMessage,
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
    SQLModel.metadata.create_all(engine, tables=[m.__table__ for m in tables])
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    for module in (conductor, conductor.graph, controls):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setenv(auto.ENABLED_ENV, "true")
    monkeypatch.setattr(auto, "_last_run", None)
    yield engine
    engine.dispose()


class Github:
    def __init__(self):
        self.labels = {"needs-human"}
        self.state = "open"
        self.body = "body"
        self.head = "a" * 40
        self.reviews: list[dict] = []
        self.unreadable = False
        self.writes: list[tuple[str, dict]] = []

    def get(self, _repo, suffix):
        if self.unreadable:
            raise ValueError("down")
        if suffix.startswith("pulls/"):
            return {"head": {"sha": self.head}}
        return {
            "number": ISSUE,
            "title": "Issue",
            "body": self.body,
            "state": self.state,
            "labels": [{"name": name} for name in sorted(self.labels)],
        }

    def list(self, _repo, suffix):
        return self.reviews if "/reviews" in suffix else []

    def write(self, _repo, suffix, payload, *, method="POST"):
        self.writes.append((suffix, payload))
        return {"id": 1} if suffix.endswith("/comments") else {}


@pytest.fixture
def github(monkeypatch):
    fake = Github()
    monkeypatch.setattr(conductor, "github_list", fake.list)
    monkeypatch.setattr(conductor, "github_get", fake.get)
    monkeypatch.setattr(landing, "github_write", fake.write)
    return fake


def configure():
    policy = {
        "repo": REPO,
        "issue_numbers": [ISSUE],
        "generation": 0,
        "max_tasks": {"delivery": 1, "advisory": 1},
        "max_turns_per_task": 20,
        "task_budget_usd": 30.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["opus", "luna"],
        "conductor_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "task_timeout_seconds": 3600,
        "max_attempts": 4,
        "intake": {"enabled": True, "refine_enabled": True},
    }
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]


def card(first="agent-ready", **extra):
    return {
        "kind": "delivery",
        "origin": {"class": "planner_pause"},
        "question": "Merge the working fix or wait for permission?",
        "reason": "The tests pass.",
        "options": [
            {
                "key": "go",
                "label": "Carry on with the working fix",
                "effect": first,
                "detail": (
                    {"scope": "The same fix."}
                    if first == "agent-ready"
                    else {"reason": "not_planned", "comment": "Not needed."}
                ),
            },
            {"key": "hold", "label": "Leave it open", "effect": "hold"},
        ],
        "pr_number": PR,
        "resolved": None,
        "issue_body_sha256": controls.issue_body_hash("body"),
        **extra,
    }


def escalate(db, document=None, *, body="Untrusted issue body.", age=3600):
    """An escalated delivery receipt; creates it on first use, re-raises after."""
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).first()
    if row is None:
        receive_issue(
            REPO,
            ISSUE,
            "Fix",
            body,
            f"https://github.com/{REPO}/issues/{ISSUE}",
            "factory:intake",
            task_class="bug-fix",
        )
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        row.state = "escalated"
        row.task_id = None
        row.task_paused = False
        row.updated_at = controls._now() - timedelta(seconds=age)
        row.escalation_json = json.dumps(document or card())
        session.add(row)
        session.commit()
        return row.id


def actions(db, action):
    with Session(db) as session:
        return [
            json.loads(r.detail_json)
            for r in session.exec(
                select(FactoryAudit).where(FactoryAudit.action == action)
            )
        ]


def tick(state="enabled"):
    auto._last_run = None
    return auto.tick(state)


def test_routine_card_defaults_to_its_recommendation_once(db, github):
    configure()
    receipt_id = escalate(db)

    assert tick() == {"applied": 1}

    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        assert row.state == "queued"
        resolved = json.loads(row.escalation_json)["resolved"]
        assert resolved["option_key"] == "go" and resolved["actor"] == auto.ACTOR
        assert json.loads(row.direction_json)["effect"] == "agent-ready"
    assert len(actions(db, auto.APPLIED)) == 1
    # A second pass finds nothing escalated, and a replay of the identical
    # request returns the stored outcome rather than applying again.
    assert tick() == {}
    decision = decisions._fields(row)["decision_id"]
    decisions.request_decision(
        receipt_id,
        decision,
        "go",
        auto.ACTOR,
        request_key=f"auto:{decision}",
        note=auto.NOTE,
    )
    assert len(actions(db, "decision_applied")) == 1


def test_flag_off_and_interval_do_nothing(db, github, monkeypatch):
    configure()
    escalate(db)
    monkeypatch.setenv(auto.ENABLED_ENV, "false")
    assert tick() == {}
    monkeypatch.setenv(auto.ENABLED_ENV, "true")
    assert auto.tick("enabled") == {"applied": 1}
    escalate(db)
    assert auto.tick("enabled") == {}  # inside the interval


def test_card_inside_the_grace_window_is_left_to_a_person(db, github):
    configure()
    escalate(db, age=10)
    assert tick() == {"grace": 1}


@pytest.mark.parametrize(
    ("document", "outcome"),
    [
        (card(first="close"), "recommendation_not_continue"),
        (card(question="Rotate the API token?"), "sensitive_scope"),
        (card(reason="Needs a destructive migration."), "sensitive_scope"),
        (card(question="Grant the agent access to production?"), "sensitive_scope"),
        (card(reason="Includes deleting stale rows."), "sensitive_scope"),
        (card(summary="Dropping the old table."), "sensitive_scope"),
        (card(dispatch_refusal={"x": 1}), "funding_gate"),
        (card(resolved={"effect": "hold"}), "not_open"),
        ({**card(), "options": card()["options"][:1]}, "invalid_options"),
    ],
)
def test_non_routine_cards_stay_gated(db, github, document, outcome):
    configure()
    escalate(db, document)
    assert tick() == {outcome: 1}
    assert actions(db, auto.APPLIED) == []


@pytest.mark.parametrize(
    ("mutate", "outcome"),
    [
        (lambda g: g.labels.add("human"), "human_owned"),
        (lambda g: g.labels.add("security-finding"), "human_owned"),
        (lambda g: setattr(g, "state", "closed"), "issue_closed"),
        (lambda g: setattr(g, "body", "edited since"), "stale_evidence"),
        (lambda g: setattr(g, "unreadable", True), "evidence_unreadable"),
        (
            lambda g: g.reviews.append(
                {"user": {"login": "r"}, "state": "CHANGES_REQUESTED"}
            ),
            "review_objection",
        ),
    ],
)
def test_live_evidence_gates(db, github, mutate, outcome):
    configure()
    escalate(db)
    mutate(github)
    assert tick() == {outcome: 1}
    assert actions(db, auto.APPLIED) == []


def test_a_later_approval_clears_an_earlier_objection(db, github):
    configure()
    escalate(db)
    github.reviews += [
        {"user": {"login": "r"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "r"}, "state": "APPROVED"},
    ]
    assert tick() == {"applied": 1}


def test_explicit_holds_and_pauses_stay_gated(db, github):
    configure()
    receipt_id = escalate(db)
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        row.direction_json = json.dumps({"landing_hold": {"by": "human"}})
        session.add(row)
        session.commit()
    assert tick() == {"explicit_hold": 1}
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        row.direction_json = None
        row.task_paused = True
        session.add(row)
        session.commit()
    assert tick() == {"receipt_held": 1}


@pytest.fixture
def runs(monkeypatch):
    """Server-observed node runs for the escalated task."""
    state = {"runs": [{"id": 1, "node_key": "implement", "status": "failed"}]}
    monkeypatch.setattr(auto, "_runs", lambda _db, _row: list(state["runs"]))
    return state


def succeed(runs, run_id):
    runs["runs"].append({"id": run_id, "node_key": "review", "status": "succeeded"})


def test_repeat_without_progress_becomes_one_repair_signal(db, github, runs):
    configure()
    escalate(db)
    assert tick() == {"applied": 1}
    escalate(db)  # the same failure comes back at the same head

    assert tick() == {"no_progress": 1}
    assert tick() == {"no_progress": 1}
    blockers = actions(db, auto.BLOCKER)
    assert len(blockers) == 1 and blockers[0]["reason"] == "no_progress"
    assert len(blockers[0]["blocker_key"]) == 64
    assert len(actions(db, auto.APPLIED)) == 1


def test_reworded_question_is_not_progress(db, github, runs):
    configure()
    escalate(db)
    assert tick() == {"applied": 1}
    escalate(db, card(question="Please approve the same fix again?"))
    assert tick() == {"no_progress": 1}
    assert len(actions(db, auto.BLOCKER)) == 1


def test_a_moved_head_alone_is_not_progress(db, github, runs):
    configure()
    escalate(db)
    assert tick() == {"applied": 1}
    github.head = "b" * 40  # moved, but nothing server-observed succeeded
    escalate(db)
    assert tick() == {"no_progress": 1}


def test_new_artifact_and_success_evidence_is_progress_until_the_cap(
    db, github, runs
):
    configure()
    escalate(db)
    assert tick() == {"applied": 1}
    github.head = "b" * 40
    succeed(runs, 5)
    escalate(db)
    assert tick() == {"applied": 1}
    github.head = "c" * 40
    succeed(runs, 9)
    escalate(db)
    assert tick() == {"applied": 1}

    github.head = "d" * 40
    succeed(runs, 12)
    escalate(db)
    assert tick() == {"auto_limit": 1}
    assert len(actions(db, auto.APPLIED)) == 3
    assert len(actions(db, auto.BLOCKER)) == 1


def test_a_different_failure_identity_is_novel(db, github, runs):
    configure()
    escalate(db)
    assert tick() == {"applied": 1}
    runs["runs"] = [{"id": 2, "node_key": "integrate", "status": "failed"}]
    escalate(db)
    assert tick() == {"applied": 1}


def test_repair_issues_never_spawn_repairs(db, github, runs):
    configure()
    escalate(db, body="<!-- factory-problem:abc -->\nrepair")
    assert tick() == {"applied": 1}
    escalate(db, body="<!-- factory-problem:abc -->\nrepair")
    assert tick() == {"no_progress": 1}
    assert actions(db, auto.BLOCKER) == []


def test_unclassified_and_historical_cards_wait_for_a_person(db, github):
    configure()
    document = card()
    del document["origin"]
    escalate(db, document)
    assert tick() == {"unclassified": 1}
    escalate(db, card(origin={"class": "funding_doubt"}))
    assert tick() == {"unclassified": 1}
    assert actions(db, auto.APPLIED) == []


def test_factory_generated_repair_refinement_defaults_to_its_recommendation(
    db, github
):
    configure()
    document = card()
    del document["origin"]
    receipt_id = escalate(
        db, document, body="<!-- factory-problem:abc -->\nrepair"
    )
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        row.task_class = "refine"
        session.add(row)
        session.commit()
    assert tick() == {"applied": 1}


@pytest.mark.parametrize(
    "direction",
    [
        {"nonrenewable_recovery": {"by": "operator"}},
        {"model_restriction": {"removed_models": ["opus"]}},
    ],
)
def test_shared_restriction_owners_gate(db, github, direction):
    configure()
    receipt_id = escalate(db)
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        row.direction_json = json.dumps(direction)
        session.add(row)
        session.commit()
    assert tick() == {"explicit_hold": 1}


def test_a_recorded_human_handoff_gates(db, github):
    configure()
    escalate(db)
    with controls._locked_session() as (session, _control):
        controls._audit(session, "operator", "human_handoff", task_id=None)
    assert tick() == {"applied": 1}  # no task identity to match, no handoff
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        row.direction_json = json.dumps({"previous_task_ids": ["t-1"]})
        session.add(row)
        session.add(
            FactoryAudit(actor="op", action="human_handoff", task_id=None, detail_json="{}")
        )
        session.commit()


def test_stopped_and_paused_lanes_do_nothing(db, github):
    configure()
    escalate(db)
    for state in ("stopped", "paused", "disabled", None):
        assert tick(state) == {}
    assert github.writes == []
    assert actions(db, auto.APPLIED) == []


def test_the_claim_revalidates_under_the_lock_if_the_lane_stops(db, github):
    configure()
    receipt_id = escalate(db)
    assert controls.set_control("stop", "operator")["ok"]
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        fields = decisions._fields(row)
    result = decisions.request_decision(
        receipt_id,
        fields["decision_id"],
        "go",
        auto.ACTOR,
        request_key="auto:direct",
        note=auto.NOTE,
    )
    assert result["state"] == "refused" and result["reason"] == "auto:lane_not_enabled"
    assert github.writes == []


def test_concurrent_considerations_apply_the_card_once(db, github, runs):
    import threading

    configure()
    receipt_id = escalate(db)
    now = controls._now()
    outcomes: list[str] = []
    barrier = threading.Barrier(4)

    def attempt():
        barrier.wait()
        try:
            outcomes.append(auto._consider(receipt_id, now))
        except Exception as exc:  # noqa: BLE001 - a lost race may raise
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # Racing callers share one request key: every one reports the single
    # stored outcome, and the effect and ledger happen exactly once.
    assert set(outcomes) <= {"applied", "unconfirmed"} and "applied" in outcomes
    assert len(actions(db, "decision_applied")) == 1
    assert len(actions(db, auto.APPLIED)) == 1


@pytest.mark.parametrize("state", ["accepted", "outcome_unknown"])
def test_an_unconfirmed_outcome_is_neither_counted_nor_a_repeated_blocker(
    db, github, monkeypatch, state
):
    configure()
    escalate(db)
    monkeypatch.setattr(
        decisions, "request_decision", lambda *a, **k: {"ok": False, "state": state}
    )
    assert tick() == {"unconfirmed": 1}
    assert tick() == {"unconfirmed": 1}
    assert actions(db, auto.APPLIED) == []
    assert actions(db, auto.BLOCKER) == []
