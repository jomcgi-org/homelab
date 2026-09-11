"""Operator decisions on a refine escalation: effects, idempotency, audit."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from swarm import factory_conductor as conductor
from swarm import factory_controls as controls
from swarm import factory_decisions as decisions
from swarm import factory_landing as landing
from swarm.factory_intake import receive_issue
from swarm.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from swarm.models import (
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
        f"sqlite:///{tmp_path / 'decisions.db'}",
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
        FactoryControl,
        FactoryReceipt,
        FactoryStart,
        FactoryAudit,
    )
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    for module in (conductor, controls):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


class Github:
    """A GitHub that records writes, so a test asserts on what really happened."""

    def __init__(self):
        self.comments: list[dict] = []
        self.writes: list[tuple[str, str, dict]] = []
        self.next_issue = 100

    def list(self, _repo, suffix):
        if "comments" in suffix and "page=1" in suffix:
            return list(self.comments)
        return []

    def write(self, repo, suffix, payload, *, method="POST"):
        self.writes.append((method, suffix, payload))
        if suffix.endswith("/comments"):
            self.comments.append({"body": payload["body"]})
            return {"id": len(self.comments)}
        if suffix == "issues":
            self.next_issue += 1
            return {"number": self.next_issue}
        return {}

    def bodies(self) -> str:
        return "\n".join(comment["body"] for comment in self.comments)


@pytest.fixture
def github(monkeypatch):
    fake = Github()
    monkeypatch.setattr(conductor, "github_list", fake.list)
    monkeypatch.setattr(conductor, "github_get", lambda *_a: {})
    monkeypatch.setattr(landing, "github_write", fake.write)
    return fake


def options(first="split"):
    head = {
        "deliver": {
            "key": "deliver",
            "label": "Deliver the /invoke path first",
            "effect": "agent-ready",
            "detail": {"scope": "Only the /invoke path."},
        },
        "close": {
            "key": "close",
            "label": "Close as superseded by #5656",
            "effect": "close",
            "detail": {"reason": "not_planned", "comment": "Superseded by #5656."},
        },
        "split": {
            "key": "split",
            "label": "Split the console out of the API",
            "effect": "split",
            "detail": {
                "children": [
                    {"title": "The console", "body": "The operator console."},
                    {"title": "The API", "body": "The decision endpoint."},
                ]
            },
        },
        "defer": {
            "key": "defer",
            "label": "Defer until the hub migration lands",
            "effect": "defer",
            "detail": {"comment": "After the hub migration."},
        },
    }[first]
    return [head, {"key": "hold", "label": "Leave it open", "effect": "hold"}]


def escalate(db, first="split", *, task_class="refine", state="succeeded"):
    """A settled refine receipt carrying an escalation, without running a node."""
    receive_issue(
        REPO,
        ISSUE,
        "Clarify delivery",
        "Untrusted issue body.",
        f"https://github.com/{REPO}/issues/{ISSUE}",
        "factory:intake",
        task_class=task_class,
    )
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        row.state = state
        row.escalation_json = json.dumps(
            {
                "recommendation": first,
                "question": "Which target is required?",
                "summary": "Two features wearing one issue number.",
                "options": options(first),
                "comment_url": f"https://github.com/{REPO}/issues/{ISSUE}#c1",
                "downgraded": False,
                "resolved": None,
            }
        )
        session.add(row)
        session.commit()
        return row.id


def audit_actions(db):
    with Session(db) as session:
        return [
            row.action
            for row in session.exec(select(FactoryAudit).order_by(FactoryAudit.id))
        ]


def escalation(db, receipt_id):
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        return json.loads(row.escalation_json)


def test_agent_ready_moves_the_labels_and_comments_the_scope(db, github):
    receipt_id = escalate(db, "deliver")
    result = decisions.apply_decision(receipt_id, "deliver", "joe@example.test")
    assert result["applied"] is True
    assert ("POST", f"issues/{ISSUE}/labels", {"labels": ["agent-ready"]}) in (
        github.writes
    )
    assert ("DELETE", f"issues/{ISSUE}/labels/needs-human", {}) in github.writes
    assert "Only the /invoke path." in github.bodies()
    resolved = escalation(db, receipt_id)["resolved"]
    assert resolved["actor"] == "joe@example.test"
    assert resolved["effects"]["labels_removed"] == ["needs-human"]
    assert "decision_applied" in audit_actions(db)


def test_close_comments_then_closes_with_the_stated_reason(db, github):
    receipt_id = escalate(db, "close")
    decisions.apply_decision(receipt_id, "close", "joe@example.test")
    patch = [write for write in github.writes if write[0] == "PATCH"]
    assert patch == [
        ("PATCH", f"issues/{ISSUE}", {"state": "closed", "state_reason": "not_planned"})
    ]
    # The comment lands before the close: a reader arriving at a closed issue
    # must find the reason already on it.
    assert github.writes[0][1].endswith("/comments")
    assert "Superseded by #5656." in github.bodies()


def test_split_opens_the_children_then_closes_the_parent(db, github):
    receipt_id = escalate(db, "split")
    result = decisions.apply_decision(receipt_id, "split", "joe@example.test")
    assert result["resolution"]["effects"]["children"] == [101, 102]
    created = [write for write in github.writes if write[1] == "issues"]
    assert [write[2]["title"] for write in created] == ["The console", "The API"]
    assert f"Split out of #{ISSUE}" in created[0][2]["body"]
    assert (
        "PATCH",
        f"issues/{ISSUE}",
        {
            "state": "closed",
            "state_reason": "not_planned",
        },
    ) in github.writes
    assert audit_actions(db).count("decision_child_created") == 2


def test_defer_moves_it_to_needs_thought_with_the_wait_condition(db, github):
    receipt_id = escalate(db, "defer")
    decisions.apply_decision(receipt_id, "defer", "joe@example.test")
    assert ("POST", f"issues/{ISSUE}/labels", {"labels": ["needs-thought"]}) in (
        github.writes
    )
    assert ("DELETE", f"issues/{ISSUE}/labels/needs-human", {}) in github.writes
    assert "After the hub migration." in github.bodies()


def test_hold_records_the_decision_and_writes_nothing(db, github):
    receipt_id = escalate(db, "deliver")
    decisions.apply_decision(receipt_id, "hold", "joe@example.test")
    assert github.writes == []
    assert escalation(db, receipt_id)["resolved"]["effect"] == "hold"


def test_the_same_option_twice_applies_once(db, github):
    receipt_id = escalate(db, "close")
    first = decisions.apply_decision(receipt_id, "close", "joe@example.test")
    writes = len(github.writes)
    second = decisions.apply_decision(receipt_id, "close", "joe@example.test")
    assert first["applied"] is True and second["applied"] is False
    assert len(github.writes) == writes
    assert second["resolution"] == first["resolution"]


def test_a_retry_after_a_failed_close_does_not_double_the_comment(db, github):
    receipt_id = escalate(db, "close")
    request = httpx.Request("PATCH", "https://api.github.com")
    response = httpx.Response(502, request=request)

    def flaky(repo, suffix, payload, *, method="POST"):
        if method == "PATCH":
            raise httpx.HTTPStatusError("boom", request=request, response=response)
        return github.write(repo, suffix, payload, method=method)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(landing, "github_write", flaky)
        with pytest.raises(decisions.DecisionError):
            decisions.apply_decision(receipt_id, "close", "joe@example.test")
    assert len(github.comments) == 1
    assert "decision_failed" in audit_actions(db)
    decisions.apply_decision(receipt_id, "close", "joe@example.test")
    # The marker on the first comment is what stops the retry writing a second.
    assert len(github.comments) == 1
    assert escalation(db, receipt_id)["resolved"]["option_key"] == "close"


def test_a_second_different_option_is_refused(db, github):
    receipt_id = escalate(db, "close")
    decisions.apply_decision(receipt_id, "close", "joe@example.test")
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.apply_decision(receipt_id, "hold", "joe@example.test")
    assert raised.value.status == 409


def test_an_unknown_option_or_receipt_is_refused(db, github):
    receipt_id = escalate(db)
    with pytest.raises(decisions.DecisionError) as unknown_option:
        decisions.apply_decision(receipt_id, "nope", "joe@example.test")
    assert unknown_option.value.status == 422
    with pytest.raises(decisions.DecisionError) as unknown_receipt:
        decisions.apply_decision(receipt_id + 999, "hold", "joe@example.test")
    assert unknown_receipt.value.status == 404


def test_chat_posts_the_question_and_requeues_the_refine(db, github):
    receipt_id = escalate(db)
    result = decisions.request_chat(
        receipt_id, "Does this cover the friends tier?", "joe@example.test"
    )
    assert result["requeued"] is True
    assert "Operator asks: Does this cover the friends tier?" in github.bodies()
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        assert row.state == "queued"
        assert row.task_id is None
    chat = escalation(db, receipt_id)["chat"]
    assert chat[0]["note"] == "Does this cover the friends tier?"
    assert chat[0]["actor"] == "joe@example.test"
    assert "decision_chat_requested" in audit_actions(db)


def test_chat_twice_asks_twice_and_a_retry_asks_once(db, github):
    receipt_id = escalate(db)
    decisions.request_chat(receipt_id, "First question", "joe@example.test")
    # The receipt is back in the queue, so the second ask is a new question on
    # the same escalation rather than a repeat of the first.
    decisions.request_chat(receipt_id, "Second question", "joe@example.test")
    assert len(github.comments) == 2
    assert len(escalation(db, receipt_id)["chat"]) == 2


def test_chat_needs_a_note_and_refuses_a_decided_escalation(db, github):
    receipt_id = escalate(db, "close")
    assert decisions.request_chat(receipt_id, "   ", "joe@example.test")["ok"] is False
    decisions.apply_decision(receipt_id, "close", "joe@example.test")
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.request_chat(receipt_id, "too late", "joe@example.test")
    assert raised.value.status == 409


def test_a_receipt_with_no_escalation_cannot_be_decided(db, github):
    receive_issue(
        REPO,
        ISSUE,
        "Clarify delivery",
        "Untrusted issue body.",
        f"https://github.com/{REPO}/issues/{ISSUE}",
        "factory:intake",
    )
    with Session(db) as session:
        receipt_id = session.exec(select(FactoryReceipt)).one().id
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.apply_decision(receipt_id, "hold", "joe@example.test")
    assert raised.value.status == 409


def test_the_board_shape_puts_open_escalations_first(db, github):
    receipt_id = escalate(db, "close")
    state = controls.status()
    assert controls.escalations(state["receipts"])[0]["open"] is True
    decisions.apply_decision(receipt_id, "close", "joe@example.test")
    view = controls.escalations(controls.status()["receipts"])[0]
    assert view["open"] is False
    assert view["resolved"]["label"] == "Close as superseded by #5656"
    assert view["options"][0]["effect"] == "close"
    # The child bodies never reach the browser; the count is what it renders.
    assert "children" in view["options"][0]
