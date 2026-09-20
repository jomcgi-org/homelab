"""Operator decisions on a refine escalation: effects, idempotency, audit."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import factory_conductor as conductor
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_decisions as decisions
from factory.orchestration import factory_funding as funding
from factory.orchestration import factory_landing as landing
from factory.orchestration import factory_refine as refine
from factory.orchestration.factory_intake import admit_next, receive_issue
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
    FactoryReviewVerdict,
    FactoryStart,
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
    for module in (conductor, conductor.graph, controls):
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
            "title": f"Issue {number}",
            "body": "body",
            "html_url": f"https://github.com/{REPO}/issues/{number}",
            "state": state,
            "labels": [{"name": name} for name in sorted(labels)],
            "user": {"login": "jomcgi", "type": "User"},
            "created_at": "2026-09-19T12:00:00Z",
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
            return {
                "number": self.next_issue,
                "title": payload["title"],
                "body": payload["body"],
                "html_url": f"https://github.com/{repo}/issues/{self.next_issue}",
                "state": "open",
                "labels": [],
                "user": {"login": "jomcgi", "type": "User"},
                "created_at": "2026-09-19T12:00:00Z",
            }
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


def test_refine_escalation_stores_the_normalized_issue_body_hash(db, monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    configure()
    receive_issue(
        REPO,
        ISSUE,
        "Clarify delivery",
        "The first line.\n\n  The second line.",
        f"https://github.com/{REPO}/issues/{ISSUE}",
        "factory:intake",
        task_class="refine",
    )
    task_id = admit_next("test")["task_id"]
    document = refine._record_escalation(
        task_id,
        {
            "recommendation": "split",
            "question": "Which target is required?",
            "summary": "Two targets.",
            "options": options("split"),
        },
        f"https://github.com/{REPO}/issues/{ISSUE}#c1",
        "The first line.\n\n  The second line.",
        downgraded=False,
    )
    assert document["issue_body_sha256"] == controls.issue_body_hash(
        "The first line. The second line."
    )


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
    with Session(db) as session:
        assert len(session.exec(select(WorkItem)).all()) == 2
        assert session.exec(select(WorkItemEdge)).all() == []


def test_split_creates_parent_edges_and_replay_does_not_duplicate_them(
    db, github, monkeypatch
):
    receipt_id = escalate(db, "split")
    with Session(db) as session:
        parent = WorkItem(
            title="parent",
            state="open",
            source_kind="github",
            trust="trusted",
            authority="github",
            github_repo=REPO,
            github_issue_number=ISSUE,
        )
        session.add(parent)
        session.flush()
        receipt = session.get(FactoryReceipt, receipt_id)
        receipt.work_item_id = parent.id
        session.add(receipt)
        session.commit()
        parent_id = parent.id

    original_close = decisions._close
    failed = False

    def fail_once(repo, number, reason):
        nonlocal failed
        if not failed:
            failed = True
            raise ValueError("close failed")
        return original_close(repo, number, reason)

    monkeypatch.setattr(decisions, "_close", fail_once)
    with pytest.raises(decisions.DecisionError):
        decisions.apply_decision(receipt_id, "split", "joe@example.test")
    result = decisions.apply_decision(receipt_id, "split", "joe@example.test")
    assert result["resolution"]["effects"]["children"] == [101, 102]
    with Session(db) as session:
        edges = session.exec(
            select(WorkItemEdge).where(
                WorkItemEdge.from_id == parent_id,
                WorkItemEdge.kind == "parent",
            )
        ).all()
        assert len(edges) == 2
        assert len({edge.to_id for edge in edges}) == 2
        assert {edge.source for edge in edges} == {"decision"}


def test_split_mint_failure_is_audited_without_failing_decision(
    db, github, monkeypatch
):
    receipt_id = escalate(db, "split")

    def fail_mint(*_args, **_kwargs):
        raise RuntimeError("database mint failed")

    monkeypatch.setattr(decisions.work_items, "mint_or_sync_from_github", fail_mint)
    result = decisions.apply_decision(receipt_id, "split", "joe@example.test")
    assert result["applied"] is True
    assert result["resolution"]["effects"]["children"] == [101, 102]
    assert audit_actions(db).count("decision_child_link_error") == 2


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


def current_decision_id():
    return controls.escalations(controls.status()["receipts"])[0]["decision_id"]


def durable_answer(
    receipt_id, identity, *, key="request-1", actor="operator", option="close"
):
    return decisions.request_decision(
        receipt_id, identity, option, actor, request_key=key
    )


def test_durable_answer_survives_receipt_replacement_and_rejects_key_reuse(db, github):
    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    first = durable_answer(receipt_id, identity)
    assert first["state"] == "completed"
    writes = list(github.writes)
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        row.escalation_json = json.dumps(
            {"question": "New", "options": options("split")}
        )
        session.add(row)
        session.commit()
    db.dispose()
    assert durable_answer(receipt_id, identity) == first
    conflict = durable_answer(receipt_id, identity, option="split")
    assert conflict["reason"] == "conflicting_decision_request"
    assert github.writes == writes
    # A genuinely new brief can choose a different option. Completed claims
    # from the old brief must not fence the replacement forever.
    new = durable_answer(
        receipt_id, current_decision_id(), key="replacement", option="split"
    )
    assert new["state"] == "completed"
    assert len(new["resolution"]["effects"]["children"]) == 2


def test_concurrent_duplicate_answer_executes_effects_once(db, github, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    started, release = Event(), Event()
    apply = decisions._apply
    calls = []

    def blocked(*args):
        calls.append(args)
        started.set()
        assert release.wait(5)
        return apply(*args)

    monkeypatch.setattr(decisions, "_apply", blocked)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(durable_answer, receipt_id, identity)
        try:
            assert started.wait(5)
            pending = durable_answer(receipt_id, identity)
            assert pending["state"] == "accepted"
            assert (
                durable_answer(receipt_id, identity, key="other")["state"] == "refused"
            )
            assert (
                durable_answer(receipt_id, identity, actor="other")["state"]
                == "refused"
            )
            with pytest.raises(decisions.DecisionError, match="unresolved outcome"):
                decisions.apply_decision(receipt_id, "close", "browser")
        finally:
            release.set()
        result = future.result()
    assert result["state"] == "completed"
    assert len(calls) == 1
    assert durable_answer(receipt_id, identity) == result


def test_generation_bump_waits_for_claimed_issue_effect(db, github, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    configure()
    receipt_id = escalate(db, "deliver", task_class="bug-fix", state="escalated")
    identity = current_decision_id()
    started, release = Event(), Event()
    apply = decisions._apply

    def blocked(*args):
        started.set()
        assert release.wait(5)
        return apply(*args)

    monkeypatch.setattr(decisions, "_apply", blocked)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            durable_answer,
            receipt_id,
            identity,
            option="deliver",
        )
        try:
            assert started.wait(5)
            changed = controls.validate_policy(
                {
                    **controls.status()["policy"],
                    "generation": 1,
                    "task_budget_usd": 31,
                }
            )
            refused = controls.set_control("configure", "operator", policy=changed)
            assert refused["reason"] == "generation_retirement_decision_in_flight"
            assert controls.status()["policy"]["generation"] == 0
            assert github.writes == []
        finally:
            release.set()
        assert future.result()["state"] == "completed"

    assert controls.set_control("configure", "operator", policy=changed)["ok"]
    with Session(db) as session:
        assert session.get(FactoryReceipt, receipt_id).state == "cancelled"


def test_interrupted_request_is_never_reexecuted(db, github, monkeypatch):
    receipt_id = escalate(db, "close")
    identity = current_decision_id()

    class ProcessLost(BaseException):
        pass

    def lost(*_args):
        raise ProcessLost()

    monkeypatch.setattr(decisions, "_apply", lost)
    with pytest.raises(ProcessLost):
        durable_answer(receipt_id, identity)
    db.dispose()
    assert durable_answer(receipt_id, identity)["state"] == "accepted"
    assert durable_answer(receipt_id, identity, key="new")["state"] == "refused"
    assert not github.writes


def test_partial_external_effect_is_durably_unknown(db, github, monkeypatch):
    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    write = github.write

    def fail_close(repo, suffix, payload, **kwargs):
        if kwargs.get("method") == "PATCH":
            raise httpx.ReadTimeout("lost response")
        return write(repo, suffix, payload, **kwargs)

    monkeypatch.setattr(landing, "github_write", fail_close)
    result = durable_answer(receipt_id, identity)
    assert result["state"] == "outcome_unknown"
    assert len(github.comments) == 1
    assert durable_answer(receipt_id, identity) == result
    assert durable_answer(receipt_id, identity, key="new")["state"] == "refused"
    with pytest.raises(decisions.DecisionError):
        decisions.request_chat(receipt_id, "try again", "browser")
    assert len(github.comments) == 1


def test_lost_completion_response_preserves_committed_result(db, github, monkeypatch):
    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    resolve = decisions._resolve

    def lose_response(*args, **kwargs):
        resolve(*args, **kwargs)
        raise RuntimeError("response lost after commit")

    monkeypatch.setattr(decisions, "_resolve", lose_response)
    result = durable_answer(receipt_id, identity)
    assert result["state"] == "completed"
    assert durable_answer(receipt_id, identity) == result


def test_stale_refusal_remains_refused_when_the_brief_changes_back(db, github):
    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    refused = durable_answer(receipt_id, "decision:" + "0" * 64)
    assert refused["state"] == "refused"
    assert (
        durable_answer(receipt_id, identity)["reason"] == "conflicting_decision_request"
    )
    assert not github.writes


def test_durable_chat_posts_and_requeues_once(db, github):
    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    configure()
    args = (receipt_id, identity, "chat", "operator")
    first = decisions.request_decision(
        *args, request_key="q", note="Which tier?", action="chat"
    )
    assert first["state"] == "completed"
    assert first["requeued"] is True
    assert (
        decisions.request_decision(
            *args, request_key="q", note="Which tier?", action="chat"
        )
        == first
    )
    assert len(github.comments) == 1
    assert len(escalation(db, receipt_id)["chat"]) == 1
    with Session(db) as session:
        assert session.get(FactoryReceipt, receipt_id).state == "queued"


def test_durable_delivery_answer_does_not_block_its_own_readmission(db, github):
    receipt_id = escalate(db, "deliver", state="escalated", task_class="bug-fix")
    configure()
    result = durable_answer(receipt_id, current_decision_id(), option="deliver")
    assert result["state"] == "completed"
    assert result["resolution"]["effects"]["readmitted"] is True


def test_fresh_context_uses_durable_exchanges_even_when_knowledge_is_down(
    db, github, monkeypatch
):
    import asyncio
    from factory.orchestration import conductor_context as context

    receipt_id = escalate(db, "close")
    durable_answer(receipt_id, current_decision_id())
    db.dispose()

    async def unavailable(query, scope, limit):
        assert scope == "repo:" + REPO
        return {"status": "unavailable", "notes": []}

    monkeypatch.setattr(context, "retrieve_knowledge", unavailable)
    result = asyncio.run(context.read_context(receipt_id, None, 5))
    assert result["receipt"]["state"] == "succeeded"
    assert result["recent_exchanges"][0]["actor"] == "operator"
    assert result["recent_exchanges"][0]["outcome"]["state"] == "completed"
    assert result["knowledge"]["status"] == "unavailable"


def test_committed_exchange_is_reported_deterministically_for_extraction(
    db, github, monkeypatch
):
    from types import SimpleNamespace
    from factory.orchestration import conductor_context as context

    receipt_id = escalate(db, "close")
    durable_answer(receipt_id, current_decision_id())
    recorded = []

    def ingest(session, **kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(raw_id="raw-1"), len(recorded) == 1

    monkeypatch.setattr("core.db.get_engine", lambda: db)
    monkeypatch.setattr("knowledge.api.ingest_raw_with_status", ingest)
    first = context.maintain_request_knowledge("operator", "request-1")
    second = context.maintain_request_knowledge("operator", "request-1")
    assert first["status"] == "queued"
    assert second["status"] == "duplicate"
    assert recorded[0] == recorded[1]
    assert recorded[0]["extra"]["scope"] == "repo:" + REPO
    assert recorded[0]["extra"]["reporter_subject"] == "operator"
    assert (
        context.maintain_request_knowledge("other", "request-1")["status"]
        == "not_applicable"
    )


def test_exact_decision_replays_its_durable_resolution(db, github):
    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    first = decisions.apply_decision(
        receipt_id, "close", "operator", expected_decision_id=identity
    )
    writes = list(github.writes)
    db.dispose()  # A fresh connection still finds the persisted resolution.
    replay = decisions.apply_decision(
        receipt_id, "close", "operator", expected_decision_id=identity
    )
    assert replay["resolution"] == first["resolution"]
    assert replay["resolution"]["decision_id"] == identity
    assert replay["applied"] is False
    assert github.writes == writes


@pytest.mark.parametrize("change", ["effect_detail", "task", "chat", "receipt"])
def test_stale_decision_is_refused_before_any_effect(db, github, change):
    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        document = json.loads(row.escalation_json)
        if change == "effect_detail":
            document["options"][0]["detail"]["comment"] = "New rationale"
        elif change == "task":
            document["task_id"] = "replacement-task"
        elif change == "chat":
            document["chat"] = [{"note": "Wait for my clarification"}]
        else:
            row.generation += 1
        row.escalation_json = json.dumps(document)
        session.add(row)
        session.commit()
    before = audit_actions(db)
    with pytest.raises(decisions.DecisionError, match="brief changed") as raised:
        decisions.apply_decision(
            receipt_id, "close", "operator", expected_decision_id=identity
        )
    assert raised.value.status == 409
    assert not github.writes
    assert audit_actions(db) == before


def test_replacement_during_effects_never_receives_the_old_resolution(
    db, github, monkeypatch
):
    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    apply = decisions._apply

    def replace_after_effects(*args):
        effects = apply(*args)
        with Session(db) as session:
            row = session.get(FactoryReceipt, receipt_id)
            document = json.loads(row.escalation_json)
            document["question"] = "A different decision"
            row.escalation_json = json.dumps(document)
            session.add(row)
            session.commit()
        return effects

    monkeypatch.setattr(decisions, "_apply", replace_after_effects)
    with pytest.raises(
        decisions.DecisionError, match="GitHub effects may have occurred"
    ):
        decisions.apply_decision(
            receipt_id, "close", "operator", expected_decision_id=identity
        )
    assert github.state == "closed"
    assert escalation(db, receipt_id)["resolved"] is None
    assert "decision_applied" not in audit_actions(db)


def test_decision_identity_is_canonical_and_receipt_scoped():
    original = {
        "id": 1,
        "repo": REPO,
        "generation": 0,
        "escalation": {"question": "Which?", "options": [], "resolved": None},
    }
    identity = controls.decision_identity(original)
    reordered = {**original, "escalation": {"options": [], "question": "Which?"}}
    assert controls.decision_identity(reordered) == identity
    assert controls.decision_identity({**original, "id": 2}) != identity
    assert controls.decision_identity({**original, "repo": "other/repo"}) != identity
    assert controls.decision_identity({"id": 1}) is None


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
    with pytest.raises(decisions.DecisionError) as raised:
        decisions.request_chat(receipt_id, "Which tier?", "joe@example.test")
    assert raised.value.status == 409
    assert "receipt generation 0" in raised.value.reason
    assert "policy is on generation 4" in raised.value.reason
    assert "no issue changes were made" in raised.value.reason
    assert github.writes == []


def test_stale_durable_decision_is_refused_before_issue_mutation(db, github):
    configure(generation=4)
    receipt_id = escalate(db, "deliver")
    card = controls.escalations(controls.status()["receipts"])[0]

    result = decisions.request_decision(
        receipt_id,
        card["decision_id"],
        "deliver",
        "joe@example.test",
        request_key="stale-answer",
    )

    assert result["state"] == "refused"
    assert result["status"] == 409
    assert "receipt generation 0" in result["reason"]
    assert "policy is on generation 4" in result["reason"]
    assert "no issue changes were made" in result["reason"]
    assert github.writes == []
    assert escalation(db, receipt_id)["resolved"] is None


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


def envelope_task(*, max_turns=6, max_attempts=1):
    policy = {
        "repo": REPO,
        "issue_numbers": [ISSUE],
        "generation": 0,
        "max_tasks": {"delivery": 1, "advisory": 1},
        "max_turns_per_task": max_turns,
        "task_budget_usd": 30.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["opus", "astra", "luna"],
        "conductor_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "task_timeout_seconds": 3600,
        "max_attempts": max_attempts,
        "intake": {"enabled": True, "refine_enabled": True},
    }
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    receive_issue(
        REPO,
        ISSUE,
        "Fix conductor livelock",
        "Keep funding bounded and closeable.",
        f"https://github.com/{REPO}/issues/{ISSUE}",
        "factory:intake",
        task_class="bug-fix",
    )
    task_id = admit_next("test")["task_id"]
    return conductor._task(task_id), policy


def add_spent_turns(db, task, count=3):
    for ordinal in range(1, count + 1):
        node_key = f"implement_spent_{ordinal}"
        assert conductor.graph.add_node(
            task["id"],
            author_kind="engine",
            author="test",
            cause_kind="test",
            cause_ref=f"spent:{ordinal}",
            stated_reason="settled fixture work",
            expected_version=conductor.graph.current_version(task["id"]),
            node_key=node_key,
            kind="work",
            prompt="Settled work",
            model="luna",
            deps=[],
            max_cost_usd=1.0,
            side_effects=True,
            max_attempts=1,
            turn_timeout_seconds=60,
        ).ok
        start_key = f"factory-node:{task['id']}:{node_key}:1"
        with Session(db) as session:
            session.add(
                SwarmNodeRun(
                    task_id=task["id"],
                    node_key=node_key,
                    attempt=1,
                    dispatch_key=start_key,
                    pin_json=json.dumps({"model": "luna"}),
                    reserved_cost_usd=1.0,
                    status="succeeded",
                    cost_usd=0.1,
                    outcome_json="{}",
                )
            )
            session.add(
                FactoryStart(
                    task_id=task["id"],
                    start_key=start_key,
                    actor="test",
                    model="luna",
                    max_cost_usd=1.0,
                    status="succeeded",
                    cost_usd=0.1,
                )
            )
            session.commit()


def plan_for_five_more():
    return {
        "action": "plan",
        "reason": "Complete the remaining five bounded steps.",
        "edits": [
            {
                "action": "add_node",
                "reason": f"step {ordinal} remains",
                "node_key": f"remaining_{ordinal}",
                "role": "implement",
                "prompt": f"Complete step {ordinal}",
                "deps": [],
                "max_attempts": 1,
            }
            for ordinal in range(1, 6)
        ],
    }


def funding_decision(**changes):
    return {
        "action": "continue",
        "reason": "The exact remaining plan is still worthwhile.",
        "next_plan": "Complete the five refused steps, then review the result.",
        "task_budget_usd": 30.0,
        "additional_work_turns": 1,
        "lease_minutes": 30,
        **changes,
    }


def settle_funding_request(task, request, decision):
    run = {
        "id": 900,
        "node_key": request["node_key"],
        "attempt": 1,
        "status": "succeeded",
        "pin": {"model": "astra"},
        "dispatch_key": request["start_key"],
        "outcome_json": json.dumps({"value": decision}),
    }
    funding.settle(task, run, request)


def test_envelope_rejection_stores_structured_deficit(db):
    task, _policy = envelope_task()
    deficit = {"turns": {"needed": 8, "allowed": 6}, "spare_turns": 3}
    conductor._reject_decision(
        task["id"],
        "decision:1",
        "plan",
        "envelope_exceeded",
        "envelope exceeded: " + json.dumps(deficit),
    )
    with Session(db) as session:
        row = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "conductor_rejected")
        ).one()
    detail = json.loads(row.detail_json)
    assert detail["deficit"] == deficit
    assert detail["reason"].startswith("envelope exceeded: ")


def test_two_matching_refusals_escalate_with_the_deficit(db, monkeypatch):
    task, policy = envelope_task()
    deficit = {
        "turns": {"needed": 8, "allowed": 6},
        "spare_turns": 3,
    }
    monkeypatch.setattr(
        conductor,
        "_envelope_refusal",
        lambda *_args, **_kwargs: "envelope exceeded: " + json.dumps(deficit),
    )
    escalations = []
    monkeypatch.setattr(
        conductor,
        "_escalate_task",
        lambda _task, decision, _cause, _runs: escalations.append(decision),
    )
    decision = plan_for_five_more()
    conductor._apply_decision(task, policy, decision, "decision:1", [])
    assert escalations == []
    conductor._apply_decision(task, policy, decision, "decision:2", [])
    assert len(escalations) == 1
    assert '"needed": 8' in escalations[0]["question"]
    assert '"allowed": 6' in escalations[0]["question"]


def test_two_different_refusal_codes_do_not_escalate(db, monkeypatch):
    task, policy = envelope_task()
    escalations = []
    monkeypatch.setattr(
        conductor,
        "_escalate_task",
        lambda *_args: escalations.append(True),
    )
    first = {
        "action": "add_node",
        "reason": "invalid bound",
        "node_key": "first",
        "role": "implement",
        "prompt": "First",
        "deps": [],
        "max_attempts": 2,
    }
    second = {
        "action": "add_node",
        "reason": "invalid model",
        "node_key": "second",
        "role": "implement",
        "prompt": "Second",
        "deps": [],
        "model": "terra",
    }
    conductor._apply_decision(task, policy, first, "decision:1", [])
    conductor._apply_decision(task, policy, second, "decision:2", [])
    assert escalations == []


def test_successful_graph_edit_resets_matching_refusals(db, monkeypatch):
    task, policy = envelope_task()
    escalations = []
    monkeypatch.setattr(
        conductor,
        "_escalate_task",
        lambda *_args: escalations.append(True),
    )
    refused = {
        "action": "add_node",
        "reason": "invalid bound",
        "node_key": "refused",
        "role": "implement",
        "prompt": "Refused",
        "deps": [],
        "max_attempts": 2,
    }
    conductor._apply_decision(task, policy, refused, "decision:1", [])
    assert conductor.graph.add_node(
        task["id"],
        author_kind="engine",
        author="test",
        cause_kind="test",
        cause_ref="progress",
        stated_reason="useful graph progress",
        expected_version=conductor.graph.current_version(task["id"]),
        node_key="implement_progress",
        kind="work",
        prompt="Useful progress",
        model="luna",
        deps=[],
        max_cost_usd=1.0,
        side_effects=True,
        max_attempts=1,
        turn_timeout_seconds=60,
    ).ok
    conductor._apply_decision(task, policy, refused, "decision:2", [])
    assert escalations == []


def test_funding_grant_uses_the_recorded_turn_deficit(db, monkeypatch):
    task, _policy = envelope_task()
    add_spent_turns(db, task)
    conductor._reject_decision(
        task["id"],
        "decision:1",
        "plan",
        "envelope_exceeded",
        'envelope exceeded: {"turns": {"allowed": 6, "needed": 8}}',
    )
    monkeypatch.setenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "true")
    monkeypatch.setattr(
        funding,
        "_issue",
        lambda _task: {
            "number": ISSUE,
            "state": "open",
            "title": "Fix conductor livelock",
            "body": "Keep funding bounded and closeable.",
            "updated_at": "now",
        },
    )
    assert funding.request(task, "Fund the recorded deficit")
    with controls._read_session() as session:
        request = funding.pending(session, task["id"])
    assert request["deficit"]["turns"] == {"allowed": 6, "needed": 8}
    settle_funding_request(task, request, funding_decision())
    with controls._read_session() as session:
        grant = funding.amendment(session, task["id"])
    assert grant["policy_overlay"]["max_task_turns_hard"] == 8


def test_deficit_grant_still_obeys_the_objective_ceiling(db, monkeypatch):
    task, _policy = envelope_task()
    add_spent_turns(db, task)
    conductor._reject_decision(
        task["id"],
        "decision:1",
        "plan",
        "envelope_exceeded",
        'envelope exceeded: {"turns": {"allowed": 6, "needed": 8}}',
    )
    monkeypatch.setenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "true")
    monkeypatch.setattr(
        funding,
        "_issue",
        lambda _task: {
            "number": ISSUE,
            "state": "open",
            "title": "Fix conductor livelock",
            "body": "Keep funding bounded and closeable.",
            "updated_at": "now",
        },
    )
    monkeypatch.setattr(
        funding,
        "objective",
        lambda _db, _task_id: {
            "repo": REPO,
            "issue_number": ISSUE,
            "task_ids": [task["id"]],
            "committed_cost_usd": 10.0,
            "ceiling_usd": funding.OBJECTIVE_CEILING_USD,
        },
    )
    assert funding.request(task, "Fund the recorded deficit")
    with controls._read_session() as session:
        request = funding.pending(session, task["id"])
    settle_funding_request(task, request, funding_decision(task_budget_usd=195.0))
    with controls._read_session() as session:
        assert funding.amendment(session, task["id"]) is None
        settled = funding.latest(session, task["id"], "funding_review_settled")
    assert settled["refusal"] == "extension exceeds objective budget"


def test_refused_eight_turn_plan_is_funded_without_a_third_refusal(db, monkeypatch):
    task, policy = envelope_task()
    add_spent_turns(db, task)
    decision = plan_for_five_more()
    conductor._apply_decision(task, policy, decision, "decision:1", [])
    with Session(db) as session:
        first = funding.latest(session, task["id"], "conductor_rejected")
    assert first["deficit"]["turns"] == {"allowed": 6, "needed": 8}
    monkeypatch.setenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "true")
    monkeypatch.setattr(
        funding,
        "_issue",
        lambda _task: {
            "number": ISSUE,
            "state": "open",
            "title": "Fix conductor livelock",
            "body": "Keep funding bounded and closeable.",
            "updated_at": "now",
        },
    )
    assert funding.request(task, "Fund the exact deficit")
    with controls._read_session() as session:
        request = funding.pending(session, task["id"])
    settle_funding_request(task, request, funding_decision())
    funded_policy = controls.task_snapshot(task["id"])["policy"]
    conductor._apply_decision(task, funded_policy, decision, "decision:2", [])
    keys = {node["node_key"] for node in conductor.graph.load_graph(task["id"])}
    assert {f"implement_remaining_{ordinal}" for ordinal in range(1, 6)} <= keys
    with Session(db) as session:
        refusals = session.exec(
            select(FactoryAudit).where(
                FactoryAudit.task_id == task["id"],
                FactoryAudit.action == "conductor_rejected",
            )
        ).all()
    assert len(refusals) == 1
