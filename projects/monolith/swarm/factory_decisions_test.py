"""Operator decisions on a refine escalation: effects, idempotency, audit."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from swarm import factory_conductor as conductor
from swarm import factory_controls as controls
from swarm import factory_decisions as decisions
from swarm import factory_landing as landing
from swarm import factory_refine as refine
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
        self.comments_by_issue: dict[int, list[dict]] = {}
        self.writes: list[tuple[str, str, dict]] = []
        self.reads: list[int] = []
        self.next_issue = 100
        # The issue as GitHub holds it, so a test can assert on the state a
        # decision left behind rather than only on the calls it made.
        self.labels: set[str] = {"needs-human"}
        self.state = "open"
        self.issue_labels: dict[int, set[str]] = {}
        self.issue_states: dict[int, str] = {}
        self.pull_requests: set[int] = set()

    def get(self, _repo, suffix):
        number = int(suffix.rsplit("/", 1)[1])
        self.reads.append(number)
        labels = (
            self.labels if number == ISSUE else self.issue_labels.get(number, set())
        )
        state = self.state if number == ISSUE else self.issue_states.get(number, "open")
        issue = {
            "number": number,
            "state": state,
            "labels": [{"name": name} for name in sorted(labels)],
        }
        if number in self.pull_requests:
            issue["pull_request"] = {"url": f"https://api.github.test/pulls/{number}"}
        return issue

    def list(self, _repo, suffix):
        if "comments" in suffix and "page=1" in suffix:
            match = re.match(r"issues/(\d+)/comments", suffix)
            if match:
                return list(self.comments_by_issue.get(int(match.group(1)), []))
        return []

    def write(self, repo, suffix, payload, *, method="POST"):
        self.writes.append((method, suffix, payload))
        if suffix.endswith("/comments"):
            comment = {"body": payload["body"]}
            self.comments.append(comment)
            number = int(suffix.split("/")[1])
            self.comments_by_issue.setdefault(number, []).append(comment)
            return {"id": len(self.comments)}
        if suffix == "issues":
            self.next_issue += 1
            return {"number": self.next_issue}
        if method == "POST" and suffix.endswith("/labels"):
            number = int(suffix.split("/")[1])
            if number == ISSUE:
                self.labels.update(payload.get("labels") or [])
            else:
                self.issue_labels.setdefault(number, set()).update(
                    payload.get("labels") or []
                )
            return {}
        if method == "DELETE" and "/labels/" in suffix:
            number = int(suffix.split("/")[1])
            labels = (
                self.labels
                if number == ISSUE
                else self.issue_labels.setdefault(number, set())
            )
            labels.discard(suffix.rsplit("/", 1)[1])
            return {}
        if method == "PATCH" and payload.get("state") == "closed":
            number = int(suffix.split("/")[1])
            if number == ISSUE:
                self.state = "closed"
            else:
                self.issue_states[number] = "closed"
            return {}
        return {}

    def bodies(self) -> str:
        return "\n".join(comment["body"] for comment in self.comments)


@pytest.fixture
def github(monkeypatch):
    fake = Github()
    monkeypatch.setattr(conductor, "github_list", fake.list)
    monkeypatch.setattr(conductor, "github_get", fake.get)
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
        "supersede": {
            "key": "supersede",
            "label": "Fold duplicates into #10",
            "effect": "supersede",
            "detail": {
                "closes": [7, 8, 9],
                "in_favour_of": 10,
                "comment": "One issue carries the implementation.",
            },
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


def configure(*, generation=0, intake_enabled=True, issues=(ISSUE,)):
    """An enabled lane whose policy would admit this issue's refine again."""
    policy = {
        "repo": REPO,
        "issue_numbers": list(issues),
        "generation": generation,
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
        "intake": {"enabled": intake_enabled, "refine_enabled": True},
    }
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]


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


def test_supersede_closes_three_issues_and_clears_the_receipt_label(db, github):
    receipt_id = escalate(db, "supersede")
    result = decisions.apply_decision(
        receipt_id, "supersede", "joe@example.test", "Keep the context together"
    )
    effects = result["resolution"]["effects"]
    assert effects == {
        "closed": [7, 8, 9],
        "skipped": [],
        "in_favour_of": 10,
        "labels_removed": ["needs-human"],
    }
    for number in (7, 8, 9):
        assert ("POST", f"issues/{number}/labels", {"labels": ["wontfix"]}) in (
            github.writes
        )
        assert (
            "PATCH",
            f"issues/{number}",
            {"state": "closed", "state_reason": "not_planned"},
        ) in github.writes
        assert len(github.comments_by_issue[number]) == 1
        body = github.comments_by_issue[number][0]["body"]
        first_line = body.splitlines()[0]
        assert first_line.startswith("<!-- factory-decision:")
        assert first_line.endswith(f":{number} -->")
        assert not any(line.startswith(":") for line in body.splitlines())
        assert f"-->:{number}" not in body
    own_close = github.writes.index(
        (
            "PATCH",
            "issues/7",
            {"state": "closed", "state_reason": "not_planned"},
        )
    )
    own_unlabel = github.writes.index(("DELETE", "issues/7/labels/needs-human", {}))
    assert own_close < own_unlabel
    assert "Operator note: Keep the context together" in github.bodies()


def test_supersede_retry_resumes_without_duplicate_comments(db, github):
    receipt_id = escalate(db, "supersede")
    request = httpx.Request("PATCH", "https://api.github.com")
    response = httpx.Response(502, request=request)
    failed = False

    def flaky(repo, suffix, payload, *, method="POST"):
        nonlocal failed
        if suffix == "issues/9" and method == "PATCH" and not failed:
            failed = True
            raise httpx.HTTPStatusError("boom", request=request, response=response)
        return github.write(repo, suffix, payload, method=method)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(landing, "github_write", flaky)
        with pytest.raises(decisions.DecisionError):
            decisions.apply_decision(receipt_id, "supersede", "joe@example.test")

    result = decisions.apply_decision(receipt_id, "supersede", "joe@example.test")
    assert result["resolution"]["effects"]["closed"] == [7, 9]
    assert result["resolution"]["effects"]["skipped"] == [
        {"number": 8, "reason": "not_open"}
    ]
    assert [len(github.comments_by_issue[number]) for number in (7, 8, 9)] == [
        1,
        1,
        1,
    ]


def test_supersede_requires_the_receipt_issue_to_close_or_survive(db, github):
    receipt_id = escalate(db, "supersede")
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        escalation_doc = json.loads(row.escalation_json)
        escalation_doc["options"][0]["detail"] = {
            "closes": [8, 9],
            "in_favour_of": 10,
        }
        row.escalation_json = json.dumps(escalation_doc)
        session.add(row)
        session.commit()
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.apply_decision(receipt_id, "supersede", "joe@example.test")
    assert raised.value.status == 422
    assert raised.value.reason == (
        "the receipt's issue must be closed or be the one favoured"
    )
    assert github.writes == []


def test_supersede_skips_pull_requests_and_closed_issues(db, github):
    receipt_id = escalate(db, "supersede")
    github.pull_requests.add(8)
    github.issue_states[9] = "closed"

    result = decisions.apply_decision(receipt_id, "supersede", "joe@example.test")

    effects = result["resolution"]["effects"]
    assert effects["closed"] == [7]
    assert effects["skipped"] == [
        {"number": 8, "reason": "pull_request"},
        {"number": 9, "reason": "not_open"},
    ]
    assert github.reads[:3] == [7, 8, 9]
    assert not any(
        write[1].startswith(("issues/8/", "issues/9/")) for write in github.writes
    )


def test_supersede_requeues_the_favoured_survivor_for_a_fresh_brief(db, github):
    configure()
    receipt_id = escalate(db, "supersede")
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        document = json.loads(row.escalation_json)
        document["options"][0]["detail"]["closes"] = [8, 9]
        document["options"][0]["detail"]["in_favour_of"] = ISSUE
        row.escalation_json = json.dumps(document)
        session.add(row)
        session.commit()

    result = decisions.apply_decision(receipt_id, "supersede", "joe@example.test")

    effects = result["resolution"]["effects"]
    assert effects["requeued"] is True
    assert effects["blocked_by"] is None
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        assert row.state == "queued"
        assert row.task_id is None
        document = json.loads(row.escalation_json)
    assert document["chat"][-1]["note"] == (
        "Operator folded in #8, #9: brief again with their scope included"
    )
    assert "Folded in by an operator decision: #8, #9." in github.bodies()
    assert ("DELETE", "issues/7/labels/needs-human", {}) in github.writes


@pytest.mark.parametrize(
    ("detail", "reason"),
    [
        ({"in_favour_of": 10}, "no issues to close"),
        ({"closes": [7]}, "no issue in favour"),
    ],
)
def test_verify_option_requires_both_supersede_fields(detail, reason):
    invalid = controls.verify_option(
        {
            "key": "supersede",
            "label": "Fold duplicates into one issue",
            "effect": "supersede",
            "detail": detail,
        },
        0,
    )
    assert reason in invalid


def test_verify_option_refuses_to_close_the_favoured_issue():
    invalid = controls.verify_option(
        {
            "key": "supersede",
            "label": "Fold duplicates into one issue",
            "effect": "supersede",
            "detail": {"closes": [7, 10], "in_favour_of": 10},
        },
        0,
    )
    assert invalid == "option 1 supersedes the issue it favours"


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
    configure()
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
    configure()
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


def second_receipt(db, first="close"):
    """A second escalated receipt, so cross-receipt collisions are visible."""
    other = ISSUE + 1
    receive_issue(
        REPO,
        other,
        "Another issue",
        "Untrusted issue body.",
        f"https://github.com/{REPO}/issues/{other}",
        "factory:intake",
        task_class="refine",
    )
    with Session(db) as session:
        row = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.issue_number == other)
        ).one()
        row.state = "succeeded"
        row.escalation_json = json.dumps(
            {
                "recommendation": first,
                "question": "And this one?",
                "summary": "A second escalation.",
                "options": options(first),
                "comment_url": f"https://github.com/{REPO}/issues/{other}#c1",
                "downgraded": False,
                "resolved": None,
            }
        )
        session.add(row)
        session.commit()
        return row.id


def test_a_claim_on_one_receipt_does_not_block_another(db, github):
    """Both receipts have task_id NULL, which is what made the audits collide."""
    first = escalate(db, "close")
    second = second_receipt(db, "close")
    decisions.apply_decision(first, "hold", "joe@example.test")
    # Same option key, different receipt. Matching claims on task_id rendered
    # IS NULL and read the first receipt's claim as this one's holder.
    result = decisions.apply_decision(second, "close", "joe@example.test")
    assert result["applied"] is True
    assert escalation(db, second)["resolved"]["option_key"] == "close"


def test_a_split_on_one_receipt_does_not_reuse_another_s_children(db, github):
    first = escalate(db, "split")
    second = second_receipt(db, "split")
    decisions.apply_decision(first, "split", "joe@example.test")
    result = decisions.apply_decision(second, "split", "joe@example.test")
    # Four distinct issues, not the first receipt's two read back by index.
    assert result["resolution"]["effects"]["children"] == [103, 104]
    created = [write for write in github.writes if write[1] == "issues"]
    assert len(created) == 4


def test_a_failed_option_does_not_lock_the_escalation(db, github):
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
    # A different option after the failure, which the claim used to refuse
    # forever because nothing retracted it.
    result = decisions.apply_decision(receipt_id, "hold", "joe@example.test")
    assert result["applied"] is True
    assert escalation(db, receipt_id)["resolved"]["option_key"] == "hold"


def test_a_failed_split_is_audited(db, github):
    """The DecisionError a split raises from inside the effect is still a failure."""
    receipt_id = escalate(db, "split")

    def no_number(repo, suffix, payload, *, method="POST"):
        if suffix == "issues":
            return {}
        return github.write(repo, suffix, payload, method=method)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(landing, "github_write", no_number)
        with pytest.raises(decisions.DecisionError):
            decisions.apply_decision(receipt_id, "split", "joe@example.test")
    assert "decision_failed" in audit_actions(db)
    # And the claim it held is superseded, so another option still works.
    assert decisions.apply_decision(receipt_id, "hold", "joe@example.test")["applied"]


def test_deciding_after_a_chat_cancels_the_re_brief(db, github):
    configure()
    receipt_id = escalate(db, "close")
    assert decisions.request_chat(receipt_id, "Which tier?", "joe@example.test")[
        "requeued"
    ]
    with Session(db) as session:
        assert session.get(FactoryReceipt, receipt_id).state == "queued"
    decisions.apply_decision(receipt_id, "close", "joe@example.test")
    with Session(db) as session:
        # Left queued, the lane would spend an advisory slot briefing an issue
        # this decision has already closed.
        assert session.get(FactoryReceipt, receipt_id).state == "succeeded"


def test_a_decision_waits_while_a_brief_is_running(db, github):
    configure()
    receipt_id = escalate(db, "close", state="admitted")
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.apply_decision(receipt_id, "close", "joe@example.test")
    assert raised.value.status == 409
    assert "decide when it settles" in raised.value.reason
    assert github.writes == []
    view = controls.escalations(controls.status()["receipts"])[0]
    assert view["briefing"] is True


def test_a_chat_the_lane_cannot_take_says_so(db, github):
    """The question is still posted, so silence would read as success."""
    # A policy that allowlists some other issue, so this one is
    # discoverable-only and intake is what would have found it.
    configure(intake_enabled=False, issues=(ISSUE + 99,))
    receipt_id = escalate(db)
    result = decisions.request_chat(receipt_id, "Which tier?", "joe@example.test")
    assert result["requeued"] is False
    assert "neither in the policy allowlist" in result["blocked_by"]
    assert "Operator asks: Which tier?" in github.bodies()
    with Session(db) as session:
        assert session.get(FactoryReceipt, receipt_id).state == "succeeded"
    view = controls.escalations(controls.status()["receipts"])[0]
    assert view["chat"][0]["requeued"] is False
    assert "neither in the policy allowlist" in view["chat"][0]["blocked_by"]


def test_a_generation_the_policy_has_moved_past_blocks_the_re_brief(db, github):
    configure(generation=4)
    receipt_id = escalate(db)
    result = decisions.request_chat(receipt_id, "Which tier?", "joe@example.test")
    assert result["requeued"] is False
    assert "generation 0 and the policy is on 4" in result["blocked_by"]


def test_the_escape_group_is_offered_on_every_unresolved_escalation(db, github):
    """No brief writes these, so the view is what has to carry them."""
    receipt_id = escalate(db, "split")
    view = controls.escalations(controls.status()["receipts"])[0]
    assert [way["key"] for way in view["escape"]] == [
        "escape:close",
        "escape:defer",
        "escape:dismiss",
    ]
    # The brief's own options are untouched, so the numbered keys still mean
    # what the brief said they meant.
    assert [option["key"] for option in view["options"]] == ["split", "hold"]
    decisions.apply_decision(receipt_id, "escape:dismiss", "joe@example.test")
    decided = controls.escalations(controls.status()["receipts"])[0]
    assert decided["escape"] == []


def test_escape_close_closes_the_issue_and_drops_needs_human(db, github):
    receipt_id = escalate(db, "split")
    result = decisions.apply_decision(
        receipt_id, "escape:close", "joe@example.test", "not worth the slot"
    )
    assert result["applied"] is True
    assert github.writes[0][1].endswith("/comments")
    assert (
        "PATCH",
        f"issues/{ISSUE}",
        {"state": "closed", "state_reason": "not_planned"},
    ) in github.writes
    # The reason is read off the option's detail rather than hardcoded in the
    # effect, so the constant cannot go dead beside a close that ignores it.
    assert (
        result["resolution"]["effects"]["reason"]
        == controls.ESCAPE_OPTIONS[0]["detail"]["reason"]
    )
    assert ("DELETE", f"issues/{ISSUE}/labels/needs-human", {}) in github.writes
    # The label comes off after the close, never before: an open issue with
    # the label gone is the one state that puts it back in front of intake.
    order = [write[0] for write in github.writes]
    assert order.index("PATCH") < order.index("DELETE")
    assert "not worth the slot" in github.bodies()
    resolved = escalation(db, receipt_id)["resolved"]
    assert resolved["option_key"] == "escape:close"
    assert resolved["effects"]["labels_removed"] == ["needs-human"]
    assert resolved["note"] == "not worth the slot"
    assert "decision_applied" in audit_actions(db)


def test_escape_close_goes_through_on_an_issue_carrying_a_protected_label(db, github):
    """`critical` downgrades a NODE's close. It does not bind the operator.

    The issue really carries the label here rather than the rule being stubbed
    out: `refine.closing_allowed` and the protected-label check are what turn
    a node's own close into this escalation in the first place, and the person
    the rule was deferring to is the one clicking now. So the assertion is on
    the end state GitHub is left in, not on the calls made to get there.
    """
    assert "critical" in refine.PROTECTED_LABELS
    github.labels.add("critical")
    receipt_id = escalate(db, "split")

    result = decisions.apply_decision(receipt_id, "escape:close", "joe@example.test")

    assert result["applied"] is True
    assert github.state == "closed"
    # The escalation label is gone and the protected one is untouched: the
    # operator closed the issue, they did not strip its classification.
    assert github.labels == {"critical"}


def test_escape_defer_swaps_needs_human_for_needs_thought(db, github):
    receipt_id = escalate(db, "split")
    decisions.apply_decision(
        receipt_id, "escape:defer", "joe@example.test", "after the hub migration"
    )
    assert ("POST", f"issues/{ISSUE}/labels", {"labels": ["needs-thought"]}) in (
        github.writes
    )
    assert ("DELETE", f"issues/{ISSUE}/labels/needs-human", {}) in github.writes
    assert [write for write in github.writes if write[0] == "PATCH"] == []
    assert "after the hub migration" in github.bodies()
    assert escalation(db, receipt_id)["resolved"]["option_key"] == "escape:defer"


def test_escape_dismiss_resolves_without_writing_to_github(db, github):
    """The card leaves the list; the issue keeps needs-human so intake skips."""
    receipt_id = escalate(db, "split")
    result = decisions.apply_decision(
        receipt_id, "escape:dismiss", "joe@example.test", "handled elsewhere"
    )
    assert result["resolution"]["effects"] == {"dismissed": True}
    assert github.writes == []
    resolved = escalation(db, receipt_id)["resolved"]
    assert resolved["option_key"] == "escape:dismiss"
    assert resolved["note"] == "handled elsewhere"
    assert controls.escalations(controls.status()["receipts"])[0]["open"] is False


def test_an_escape_repeated_returns_the_first_result_rather_than_doubling(db, github):
    receipt_id = escalate(db, "split")
    first = decisions.apply_decision(receipt_id, "escape:close", "joe@example.test")
    writes = list(github.writes)
    second = decisions.apply_decision(receipt_id, "escape:close", "joe@example.test")
    assert first["applied"] is True
    assert second["applied"] is False
    assert second["resolution"]["decided_at"] == first["resolution"]["decided_at"]
    assert github.writes == writes
    assert audit_actions(db).count("decision_applied") == 1


def test_an_escape_retried_after_a_failure_does_not_comment_twice(
    db, github, monkeypatch
):
    """The marker is keyed on the receipt and the escape key, like any option."""
    receipt_id = escalate(db, "split")
    calls = {"n": 0}
    real = github.write

    def flaky(repo, suffix, payload, *, method="POST"):
        if method == "PATCH":
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.HTTPError("boom")
        return real(repo, suffix, payload, method=method)

    monkeypatch.setattr(landing, "github_write", flaky)
    with pytest.raises(decisions.DecisionError):
        decisions.apply_decision(receipt_id, "escape:close", "joe@example.test")
    assert "decision_failed" in audit_actions(db)
    comments = len(github.comments)
    result = decisions.apply_decision(receipt_id, "escape:close", "joe@example.test")
    assert result["applied"] is True
    assert len(github.comments) == comments


def test_a_second_different_escape_is_refused_once_one_is_decided(db, github):
    """The close is final, so the defer behind it is refused rather than run.

    A dismiss is the exception and gets its own tests: it wrote nothing, so
    there is nothing for the second decision to contradict.
    """
    receipt_id = escalate(db, "split")
    decisions.apply_decision(receipt_id, "escape:close", "joe@example.test")
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.apply_decision(receipt_id, "escape:defer", "joe@example.test")
    assert raised.value.status == 409
    assert "escape:close" in raised.value.reason
    assert ("POST", f"issues/{ISSUE}/labels", {"labels": ["needs-thought"]}) not in (
        github.writes
    )


def test_an_escape_waits_while_a_brief_is_running(db, github):
    configure()
    receipt_id = escalate(db, "split", state="admitted")
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.apply_decision(receipt_id, "escape:close", "joe@example.test")
    assert raised.value.status == 409
    assert github.writes == []


def test_an_unknown_escape_key_is_still_refused(db, github):
    receipt_id = escalate(db, "split")
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.apply_decision(receipt_id, "escape:nuke", "joe@example.test")
    assert raised.value.status == 422


def test_a_dismiss_is_not_the_end_of_it_and_a_real_decision_lands_over_it(db, github):
    """One keypress with no confirmation must not be the last word.

    A dismiss writes nothing to GitHub, so the issue is untouched and there is
    nothing for a later decision to contradict. Every other resolution stays
    final: see the test below.
    """
    receipt_id = escalate(db, "close")
    decisions.apply_decision(receipt_id, "escape:dismiss", "joe@example.test")
    assert github.writes == []

    result = decisions.apply_decision(receipt_id, "close", "joe@example.test")

    assert result["applied"] is True
    assert github.state == "closed"
    # The decision replaces the dismiss on the record rather than sitting
    # behind it, so the ledger says what actually happened to the issue.
    resolved = escalation(db, receipt_id)["resolved"]
    assert resolved["option_key"] == "close"
    assert audit_actions(db).count("decision_applied") == 2


def test_a_dismissed_escalation_can_still_be_asked_about(db, github):
    """The chat request is the way back from a card you cleared by mistake."""
    configure()
    receipt_id = escalate(db)
    decisions.apply_decision(receipt_id, "escape:dismiss", "joe@example.test")

    result = decisions.request_chat(receipt_id, "Which tier?", "joe@example.test")

    assert result["requeued"] is True
    assert "Operator asks: Which tier?" in github.bodies()
    with Session(db) as session:
        assert session.get(FactoryReceipt, receipt_id).state == "queued"


def test_a_real_decision_is_still_the_last_word(db, github):
    """The non-terminal rule is the dismiss alone, not resolutions in general."""
    receipt_id = escalate(db, "close")
    decisions.apply_decision(receipt_id, "close", "joe@example.test")
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.apply_decision(receipt_id, "escape:dismiss", "joe@example.test")
    assert raised.value.status == 409
    with pytest.raises(decisions.DecisionError) as chat:
        decisions.request_chat(receipt_id, "Which tier?", "joe@example.test")
    assert chat.value.status == 409


def test_a_dismiss_repeated_returns_the_first_one_rather_than_a_second(db, github):
    receipt_id = escalate(db, "split")
    first = decisions.apply_decision(receipt_id, "escape:dismiss", "joe@example.test")
    second = decisions.apply_decision(receipt_id, "escape:dismiss", "joe@example.test")
    assert first["applied"] is True
    assert second["applied"] is False
    assert second["resolution"]["decided_at"] == first["resolution"]["decided_at"]
    assert audit_actions(db).count("decision_applied") == 1
