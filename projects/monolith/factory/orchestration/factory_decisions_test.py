"""Operator decisions on a refine escalation: effects, idempotency, audit."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory.execution.models import AgentSession, PendingMessage
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
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    for module in (conductor, conductor.graph, controls):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    # Merit enrichment now runs on every eligible request. Keep its external
    # evidence deterministic; dedicated evidence tests exercise the reader.
    from factory.orchestration import factory_judge

    monkeypatch.setattr(factory_judge, "merit_evidence", lambda _task, _runs: MERIT)
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
        document = {
            "recommendation": first,
            "question": "Which target is required?",
            "summary": "Two features wearing one issue number.",
            "options": options(first),
            "comment_url": f"https://github.com/{REPO}/issues/{ISSUE}#c1",
            "downgraded": False,
            "resolved": None,
            "conversation": [
                {
                    "message_id": "factory-brief:test-task",
                    "role": "conductor",
                    "actor": "factory:refine",
                    "source": "factory_brief",
                    "timestamp": "2026-09-23T00:00:00+00:00",
                    "task_id": "test-task",
                    "decision_id": None,
                    "request_key": None,
                    "epistemic_status": "suggestion_or_hypothesis",
                    "text": (
                        "Two features wearing one issue number.\n\n"
                        "Which target is required?"
                    ),
                    "summary": "Two features wearing one issue number.",
                    "evidence": [f"https://github.com/{REPO}/issues/{ISSUE}#c1"],
                }
            ],
        }
        document["conversation"][0]["decision_id"] = controls.decision_identity(
            {
                "id": row.id,
                "repo": row.repo,
                "generation": row.generation,
                "escalation": document,
            }
        )
        row.escalation_json = json.dumps(document)
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
    brief = document["conversation"][0]
    assert brief["role"] == "conductor"
    assert brief["source"] == "factory_brief"
    assert brief["message_id"] == f"factory-brief:{task_id}:{brief['decision_id']}"
    assert brief["decision_id"].startswith("decision:")
    updated_artifact = {
        "recommendation": "split",
        "question": "Which target is required now?",
        "summary": "The brief changed after operator input.",
        "options": options("split"),
    }
    updated = refine._record_escalation(
        task_id,
        updated_artifact,
        f"https://github.com/{REPO}/issues/{ISSUE}#c2",
        "The first line.\n\n  The second line.",
        downgraded=False,
    )
    replayed = refine._record_escalation(
        task_id,
        updated_artifact,
        f"https://github.com/{REPO}/issues/{ISSUE}#c2",
        "The first line.\n\n  The second line.",
        downgraded=False,
    )
    assert len(updated["conversation"]) == 2
    assert len(replayed["conversation"]) == 2
    assert len({item["message_id"] for item in updated["conversation"]}) == 2


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
    result = durable_chat(db, receipt_id, "Does this cover the friends tier?")
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
    durable_chat(db, receipt_id, "First question", key="first")
    # The receipt is back in the queue, so the second ask is a new question on
    # the same escalation rather than a repeat of the first.
    durable_chat(db, receipt_id, "Second question", key="second")
    assert len(github.comments) == 2
    assert len(escalation(db, receipt_id)["chat"]) == 2


def test_chat_needs_a_note_and_refuses_a_decided_escalation(db, github):
    receipt_id = escalate(db, "close")
    with pytest.raises(ValueError, match="non-empty note"):
        durable_chat(db, receipt_id, "   ")
    decisions.apply_decision(receipt_id, "close", "joe@example.test")
    refused = durable_chat(db, receipt_id, "too late")
    assert refused["state"] == "refused"
    assert refused["status"] == 409


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


def recovery_operator(**changes):
    from auth.api import Authority, Principal, PrincipalKind

    fields = dict(
        subject="reconciler",
        actor=(),
        scope=(),
        groups=("operators",),
        email=None,
        authority=Authority.STANDING,
        kind=PrincipalKind.HUMAN,
    )
    return Principal(**{**fields, **changes})


def abandoned_request(db, github, monkeypatch):
    receipt_id = escalate(db, "close")
    identity = current_decision_id()

    class Lost(BaseException):
        pass

    with monkeypatch.context() as patch:

        def lose(*_args):
            raise Lost()

        patch.setattr(decisions, "_apply", lose)
        with pytest.raises(Lost):
            durable_answer(receipt_id, identity)
    # Base failure: a new explicitly authorized answer stays refused forever.
    assert (
        durable_answer(receipt_id, identity, key="before-settlement")["state"]
        == "refused"
    )
    assert not github.writes
    return receipt_id, identity


def recovery_body(db, receipt_id, *, conclusion="no_effects", key="settle-1"):
    from factory.orchestration.decision_recovery import Settlement, unresolved

    with Session(db) as session:
        view = unresolved(session, receipt_id)["items"][0]
    return Settlement(
        actor=view["actor"],
        request_key=view["request_key"],
        decision_id=view["decision_id"],
        expected_audit_id=view["audit_id"],
        expected_sha256=view["sha256"],
        settlement_key=key,
        evidence=dict(
            observed_at=controls._now(),
            cessation_confirmed=True,
            cessation="Exact process shutdown audit; all old replicas stopped.",
            effects="Fresh complete issue, comment, label and child audit reads.",
            conclusion=conclusion,
        ),
    )


def test_recovery_no_effects_allows_only_a_new_explicit_answer(db, github, monkeypatch):
    from factory.orchestration import decision_recovery as recovery

    receipt_id, identity = abandoned_request(db, github, monkeypatch)
    body = recovery_body(db, receipt_id)
    inspection = recovery.inspect_request(
        receipt_id, body.expected_audit_id, recovery_operator()
    )
    assert inspection["original"]["request"]["receipt_id"] == receipt_id
    assert inspection["latest"]["result"]["state"] == "accepted"
    result = recovery.settle(receipt_id, body, recovery_operator())
    assert result["state"] == "abandoned"
    settled = recovery.inspect_request(
        receipt_id, body.expected_audit_id, recovery_operator()
    )
    assert settled["original"]["result"]["state"] == "accepted"
    assert settled["latest"]["result"] == result
    assert settled["settlements"][0]["actor"] == "reconciler"
    assert settled["settlements"][0]["settlement"][
        "evidence"
    ] == body.evidence.model_dump(mode="json")
    assert not settled["settlements_truncated"]
    assert not github.writes
    assert recovery.settle(receipt_id, body, recovery_operator()) == result
    assert durable_answer(receipt_id, identity) == result
    assert (
        durable_answer(receipt_id, identity, option="hold")["reason"]
        == "conflicting_decision_request"
    )
    assert (
        durable_answer(receipt_id, identity, key="explicit-later")["state"]
        == "completed"
    )
    assert len(github.comments) == 1
    # Same settlement replay is the acknowledgement, even after later state changes.
    assert recovery.settle(receipt_id, body, recovery_operator()) == result


def test_recovery_unknown_never_unlocks_or_replays(db, github, monkeypatch):
    from factory.orchestration import decision_recovery as recovery

    receipt_id, identity = abandoned_request(db, github, monkeypatch)
    body = recovery_body(db, receipt_id, conclusion="unknown")
    body.evidence.cessation_confirmed = False
    result = recovery.settle(receipt_id, body, recovery_operator())
    assert result["state"] == "outcome_unknown"
    assert durable_answer(receipt_id, identity) == result
    assert (
        durable_answer(receipt_id, identity, key="still-fenced")["state"] == "refused"
    )
    with Session(db) as session:
        assert decisions._unsettled_legacy_claim(session, receipt_id, identity)
    assert not github.writes


def test_recovery_projects_exact_predecessor_and_preserves_other_receipts(
    db, github, monkeypatch
):
    from factory.orchestration import decision_recovery as recovery, mcp

    receipt_id, _identity = abandoned_request(db, github, monkeypatch)
    other = receive_issue(
        REPO,
        ISSUE + 1,
        "Unrelated",
        "body",
        f"https://github.com/{REPO}/issues/{ISSUE + 1}",
        "intake",
    )
    with Session(db) as session:
        before = session.get(FactoryReceipt, other["receipt"]["id"]).model_dump()
    cards = mcp._escalations_payload(True)["escalations"]
    card = next(card for card in cards if card["receipt_id"] == receipt_id)
    view = card["unresolved_requests"]
    assert view["total"] == 1 and not view["truncated"]
    item = view["items"][0]
    assert item["actor"] == "operator" and item["request_key"] == "request-1"
    assert item["state"] == "accepted" and item["created_at"] and item["updated_at"]
    assert str(item["audit_id"]) in item["inspection_path"]
    recovery.settle(receipt_id, recovery_body(db, receipt_id), recovery_operator())
    with Session(db) as session:
        assert (
            session.get(FactoryReceipt, other["receipt"]["id"]).model_dump() == before
        )
        assert not recovery.unresolved(session, other["receipt"]["id"])["items"]


def test_recovery_partial_post_write_failure_remains_unknown(db, github, monkeypatch):
    from factory.orchestration import decision_recovery as recovery

    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    write = github.write

    def timeout(repo, suffix, payload, **kwargs):
        if kwargs.get("method") == "PATCH":
            raise httpx.ReadTimeout("response lost after comment write")
        return write(repo, suffix, payload, **kwargs)

    monkeypatch.setattr(landing, "github_write", timeout)
    assert durable_answer(receipt_id, identity)["state"] == "outcome_unknown"
    assert len(github.comments) == 1
    recovery.settle(
        receipt_id,
        recovery_body(db, receipt_id, conclusion="unknown"),
        recovery_operator(),
    )
    assert durable_answer(receipt_id, identity, key="new-answer")["state"] == "refused"
    assert len(github.comments) == 1


def test_recovery_adopts_exact_durable_completion_without_writes(db, github):
    from factory.orchestration import decision_recovery as recovery

    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    completed = durable_answer(receipt_id, identity)
    writes = list(github.writes)
    # Historical result-loss shape: local resolution is durable, request receipt uncertain.
    with controls._locked_session() as (session, _control):
        request = controls._request_records(session)[("operator", "request-1")][
            "request"
        ]
        decisions._request_event(
            session, "operator", request, {"state": "outcome_unknown"}
        )
    result = recovery.settle(
        receipt_id,
        recovery_body(db, receipt_id, conclusion="completed"),
        recovery_operator(),
    )
    assert result["state"] == "completed"
    assert result["resolution"] == completed["resolution"]
    assert not result["applied"]
    assert github.writes == writes


@pytest.mark.parametrize(
    "change",
    [
        "actor",
        "receipt",
        "audit",
        "hash",
        "decision",
        "generation",
        "hold",
        "landing_hold",
        "cancellation",
        "nonrenewable",
        "stale",
        "future",
        "unavailable",
        "cessation",
        "completion",
        "partial",
    ],
)
def test_recovery_refuses_mismatched_or_unproven_evidence(
    db, github, monkeypatch, change
):
    from datetime import timedelta
    from factory.orchestration import decision_recovery as recovery

    receipt_id, identity = abandoned_request(db, github, monkeypatch)
    body = recovery_body(db, receipt_id)
    target = receipt_id
    if change == "actor":
        body.actor = "some-other-operator"
    elif change == "receipt":
        target += 1
    elif change == "audit":
        body.expected_audit_id += 1
    elif change == "hash":
        body.expected_sha256 = "0" * 64
    elif change == "decision":
        body.decision_id = "decision:" + "0" * 64
    elif change == "stale":
        body.evidence.observed_at = controls._now() - timedelta(minutes=6)
    elif change == "future":
        body.evidence.observed_at = controls._now() + timedelta(minutes=1)
    elif change == "unavailable":
        body.evidence.effects = " "
    elif change == "cessation":
        body.evidence.cessation_confirmed = False
    elif change == "completion":
        body.evidence.conclusion = "completed"
    else:
        with controls._locked_session() as (session, control):
            row = session.get(FactoryReceipt, receipt_id)
            if change == "generation":
                control.policy_json = json.dumps({"generation": 2})
            elif change == "hold":
                row.task_paused = True
            elif change == "landing_hold":
                row.direction_json = json.dumps({"landing_hold": {"actor": "operator"}})
            elif change == "nonrenewable":
                row.direction_json = json.dumps({"nonrenewable_recovery": {}})
            elif change == "cancellation":
                row.cancellation_requested = True
            elif change == "partial":
                controls._audit(
                    session,
                    "operator",
                    "decision_child_created",
                    receipt_id=receipt_id,
                    decision_id=identity,
                    child_number=99,
                )
    with pytest.raises(ValueError):
        recovery.settle(target, body, recovery_operator())
    assert (
        durable_answer(receipt_id, identity, key="after-refusal")["state"] == "refused"
    )
    assert not github.writes


def test_recovery_conflicting_settlement_keys_and_stale_result(db, github, monkeypatch):
    from factory.orchestration import decision_recovery as recovery

    receipt_id, _identity = abandoned_request(db, github, monkeypatch)
    body = recovery_body(db, receipt_id, conclusion="unknown")
    recovery.settle(receipt_id, body, recovery_operator())
    with pytest.raises(ValueError, match="stale request"):
        recovery.settle(
            receipt_id,
            body.model_copy(update={"settlement_key": "different"}),
            recovery_operator(),
        )
    body.evidence.conclusion = "no_effects"
    with pytest.raises(ValueError, match="conflicting settlement key"):
        recovery.settle(receipt_id, body, recovery_operator())


def test_recovery_live_execution_race_is_targeted(db, github, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from factory.orchestration import decision_recovery as recovery

    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    started, release = Event(), Event()
    apply = decisions._apply

    def blocked(*args):
        started.set()
        assert release.wait(5)
        return apply(*args)

    monkeypatch.setattr(decisions, "_apply", blocked)
    with ThreadPoolExecutor() as pool:
        running = pool.submit(durable_answer, receipt_id, identity)
        assert started.wait(5)
        try:
            body = recovery_body(db, receipt_id)
            assert durable_answer(receipt_id, identity)["state"] == "accepted"
            with pytest.raises(ValueError, match="execution is live"):
                recovery.settle(receipt_id, body, recovery_operator())
            with recovery.execution_lock(receipt_id + 1) as acquired:
                assert acquired
        finally:
            release.set()
        assert running.result()["state"] == "completed"
    with pytest.raises(ValueError, match="stale request"):
        recovery.settle(receipt_id, body, recovery_operator())


def test_recovery_late_resolution_cannot_overwrite_abandonment(db, github, monkeypatch):
    from factory.orchestration import decision_recovery as recovery

    receipt_id, identity = abandoned_request(db, github, monkeypatch)
    body = recovery_body(db, receipt_id)
    result = recovery.settle(receipt_id, body, recovery_operator())
    with Session(db) as session:
        request = controls._request_records(session)[("operator", "request-1")][
            "request"
        ]
    with pytest.raises(decisions.DecisionError, match="late completion"):
        decisions._resolve(
            receipt_id,
            options("close")[0],
            "operator",
            None,
            {},
            expected_decision_id=identity,
            request=request,
        )
    assert durable_answer(receipt_id, identity) == result
    assert not github.writes


@pytest.mark.parametrize("settled", [False, True])
def test_recovery_checks_retained_task_quiescence(db, github, monkeypatch, settled):
    from factory.orchestration import decision_recovery as recovery

    receipt_id, _identity = abandoned_request(db, github, monkeypatch)
    with controls._locked_session() as (session, _control):
        session.add(
            SwarmTask(
                id="retained",
                task_text="old refine",
                conductor_model="opus",
                settled_at=controls._now() if settled else None,
            )
        )
        session.flush()
        session.get(FactoryReceipt, receipt_id).task_id = "retained"
    body = recovery_body(db, receipt_id)
    if settled:
        assert (
            recovery.settle(receipt_id, body, recovery_operator())["state"]
            == "abandoned"
        )
    else:
        with pytest.raises(ValueError, match="active task"):
            recovery.settle(receipt_id, body, recovery_operator())
    assert not github.writes


@pytest.mark.parametrize("kind", ["delegated", "workload", "no_group"])
def test_recovery_requires_standing_operator_in_core(db, github, monkeypatch, kind):
    from auth.api import Authority, PrincipalKind
    from factory.orchestration import decision_recovery as recovery

    receipt_id, _identity = abandoned_request(db, github, monkeypatch)
    body = recovery_body(db, receipt_id)
    changes = {
        "delegated": {"authority": Authority.DELEGATED},
        "workload": {"kind": PrincipalKind.WORKLOAD},
        "no_group": {"groups": ()},
    }[kind]
    principal = recovery_operator(**changes)
    with pytest.raises(ValueError, match="standing operator"):
        recovery.inspect_request(receipt_id, body.expected_audit_id, principal)
    with pytest.raises(ValueError, match="standing operator"):
        recovery.settle(receipt_id, body, principal)


def durable_chat(db, receipt_id, note, actor="joe@example.test", *, key="chat-1"):
    with Session(db) as session:
        identity = decisions._fields(session.get(FactoryReceipt, receipt_id))[
            "decision_id"
        ]
    return decisions.request_decision(
        receipt_id, identity, "chat", actor, request_key=key, note=note, action="chat"
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
    assert durable_chat(db, receipt_id, "try again", "browser")["state"] == "refused"
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


def test_chat_cooldown_before_comment_is_a_durable_refusal(db, github, monkeypatch):
    from factory.orchestration.github_read_cooldown import GitHubReadDeferred

    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    configure()
    args = (receipt_id, identity, "chat", "operator")

    def deferred(*_args):
        raise GitHubReadDeferred("cooldown")

    monkeypatch.setattr(conductor, "github_list", deferred)
    result = decisions.request_decision(
        *args, request_key="read-refused", note="Repository only?", action="chat"
    )
    assert result["state"] == "refused"
    assert result["reason"] == "github_read_deferred_before_effects"
    assert result["effects_started"] is False
    assert not github.writes
    assert not escalation(db, receipt_id).get("chat")
    with Session(db) as session:
        assert session.get(FactoryReceipt, receipt_id).state == "succeeded"
        assert not controls._pending_request(session, receipt_id)
    # The refused key stays refused after the cooldown, even across readers.
    monkeypatch.setattr(conductor, "github_list", github.list)
    db.dispose()
    assert (
        decisions.request_decision(
            *args, request_key="read-refused", note="Repository only?", action="chat"
        )
        == result
    )
    assert not github.writes
    # A new explicit request can proceed; the refusal itself never retries.
    next_result = decisions.request_decision(
        *args, request_key="new-request", note="Repository only?", action="chat"
    )
    assert next_result["state"] == "completed"
    assert len(github.comments) == 1


def test_chat_write_exception_stays_unknown_even_if_named_read_deferred(
    db, github, monkeypatch
):
    from factory.orchestration.github_read_cooldown import GitHubReadDeferred

    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    write = github.write

    def wrote_then_failed(*args, **kwargs):
        write(*args, **kwargs)
        raise GitHubReadDeferred("raised after write")

    monkeypatch.setattr(landing, "github_write", wrote_then_failed)
    args = (receipt_id, identity, "chat", "operator")
    result = decisions.request_decision(
        *args, request_key="unknown", note="Repository only?", action="chat"
    )
    assert result["state"] == "outcome_unknown"
    assert len(github.comments) == 1
    assert (
        decisions.request_decision(
            *args, request_key="unknown", note="Repository only?", action="chat"
        )
        == result
    )
    assert (
        decisions.request_decision(
            *args, request_key="new-request", note="Repository only?", action="chat"
        )["state"]
        == "refused"
    )
    with Session(db) as session:
        assert controls._pending_request(session, receipt_id)
    assert len(github.comments) == 1


def test_durable_delivery_answer_does_not_block_its_own_readmission(db, github):
    receipt_id = escalate(db, "deliver", state="escalated", task_class="bug-fix")
    configure()
    result = durable_answer(receipt_id, current_decision_id(), option="deliver")
    assert result["state"] == "completed"
    assert result["resolution"]["effects"]["readmitted"] is True


def test_fresh_context_uses_durable_requests_even_when_knowledge_is_down(
    db, github, monkeypatch
):
    import asyncio
    from factory.orchestration import conductor_context as context

    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    durable_answer(receipt_id, identity)
    durable_answer(receipt_id, identity, key="request-2")
    db.dispose()

    async def unavailable(query, scopes, limit):
        assert scopes == (
            f"personal:operator:factory-receipt:{receipt_id}",
            f"session:factory-receipt:{receipt_id}",
        )
        return {"status": "unavailable", "notes": []}

    monkeypatch.setattr(context, "retrieve_knowledge", unavailable)
    result = asyncio.run(context.read_context(receipt_id, None, 5, actor="operator"))
    assert result["receipt"]["state"] == "succeeded"
    assert [item["role"] for item in result["recent_exchanges"]] == [
        "conductor",
        "operator",
    ]
    assert result["recent_exchanges"][-1]["message_id"] == (
        "factory-request:operator:request-1"
    )
    assert result["recent_exchanges"][-1]["epistemic_status"] == "approved"
    assert result["recent_requests"][0]["actor"] == "operator"
    assert result["recent_requests"][0]["outcome"]["state"] == "completed"
    assert result["knowledge"]["status"] == "unavailable"


def test_committed_exchange_is_reported_deterministically_for_extraction(
    db, github, monkeypatch
):
    from types import SimpleNamespace
    from factory.orchestration import conductor_context as context

    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    durable_answer(receipt_id, identity)
    durable_answer(receipt_id, identity, key="request-2")
    recorded = []

    def ingest(session, **kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(raw_id="raw-1"), len(recorded) == 1

    monkeypatch.setattr("core.db.get_engine", lambda: db)
    monkeypatch.setattr("knowledge.api.ingest_raw_with_status", ingest)
    first = context.maintain_request_knowledge("operator", "request-1")
    second = context.maintain_request_knowledge("operator", "request-1")
    retelling = context.maintain_request_knowledge("operator", "request-2")
    assert first["status"] == "queued"
    assert second["status"] == "duplicate"
    assert retelling["status"] == "duplicate"
    assert recorded[0] == recorded[1]
    assert recorded[0]["content"] == recorded[2]["content"]
    assert recorded[0]["extra"]["scope"] == (
        f"personal:operator:factory-receipt:{receipt_id}"
    )
    assert recorded[0]["extra"]["reporter_subject"] == "operator"
    assert (
        context.maintain_request_knowledge("other", "request-1")["status"]
        == "not_applicable"
    )


def test_fresh_context_keeps_operator_history_isolated_and_epistemic(
    db, github, monkeypatch
):
    import asyncio
    from factory.orchestration import conductor_context as context

    receipt_id = escalate(db, "close")
    brief_identity = current_decision_id()
    configure()
    decisions.request_decision(
        receipt_id,
        brief_identity,
        "chat",
        "operator-a",
        request_key="same-key",
        note="Operator A constraint.",
        action="chat",
    )
    decisions.request_decision(
        receipt_id,
        current_decision_id(),
        "chat",
        "operator-b",
        request_key="same-key",
        note="Operator B private constraint.",
        action="chat",
    )

    async def unavailable(*_args):
        return {"status": "unavailable", "notes": []}

    monkeypatch.setattr(context, "retrieve_knowledge", unavailable)
    first = asyncio.run(context.read_context(receipt_id, None, 5, actor="operator-a"))
    second = asyncio.run(context.read_context(receipt_id, None, 5, actor="operator-b"))

    # Private chat views must still name the same actionable durable brief.
    assert first["decision"]["decision_id"] == current_decision_id()
    assert second["decision"]["decision_id"] == current_decision_id()
    assert [entry["note"] for entry in first["decision"]["chat"]] == [
        "Operator A constraint."
    ]
    assert [entry["note"] for entry in second["decision"]["chat"]] == [
        "Operator B private constraint."
    ]

    assert [item["actor"] for item in first["recent_exchanges"]] == [
        "factory:refine",
        "operator-a",
    ]
    assert [item["actor"] for item in second["recent_exchanges"]] == [
        "factory:refine",
        "operator-b",
    ]
    exchange = first["recent_exchanges"][1]
    assert exchange["source"] == "factory_decision"
    assert exchange["epistemic_status"] == "operator_input"
    assert exchange["references"]["receipt_id"] == receipt_id
    assert exchange["message_id"] == "factory-request:operator-a:same-key"
    assert first["recent_exchanges"][0]["references"]["decision_id"] == (brief_identity)
    assert first["recent_exchanges"][0]["epistemic_status"] == (
        "suggestion_or_hypothesis"
    )
    assert first["recent_requests"][0]["kind"] == "operator_question"
    assert first["decision"]["recommendation_status"] == "conductor_suggestion"
    assert first["decision"]["resolution_status"] is None
    assert first["summaries"][0]["evidence"] == ["factory-brief:test-task"]
    assert first["summaries"][1]["evidence"] == ["factory-request:operator-a:same-key"]


def test_fresh_context_without_chat_has_an_actionable_decision_id(db, github):
    from factory.orchestration import conductor_context as context

    receipt_id = escalate(db, "close")
    configure()
    identity = current_decision_id()
    fresh = context._factory_context(receipt_id, actor="operator")
    assert fresh["decision"]["decision_id"] == identity
    assert fresh["decision"]["chat"] == []
    result = decisions.request_decision(
        receipt_id,
        fresh["decision"]["decision_id"],
        "chat",
        "operator",
        request_key="from-fresh-context",
        note="Keep implementation default-off.",
        action="chat",
    )
    assert result["state"] == "completed"
    assert len(github.comments) == 1


def test_legacy_decision_path_triggers_receipt_knowledge_maintenance(
    db, github, monkeypatch
):
    from factory.orchestration import conductor_context as context

    receipt_id = escalate(db, "close")
    configure()
    maintained = []
    monkeypatch.setattr(
        context,
        "report_receipt_with_deadline",
        lambda target: maintained.append(target) or {"status": "queued"},
    )

    assert decisions.apply_decision(receipt_id, "close", "operator")["applied"]
    assert maintained == [receipt_id]


def test_pending_question_survives_fresh_context_with_current_queue_state(
    db, github, monkeypatch
):
    import asyncio
    from factory.orchestration import conductor_context as context

    configure()
    receipt_id = escalate(db, "close")
    result = decisions.request_decision(
        receipt_id,
        current_decision_id(),
        "chat",
        "operator",
        request_key="question-1",
        note="Which retention scope is intended?",
        action="chat",
    )
    assert result["state"] == "completed"

    async def unavailable(*_args):
        return {"status": "unavailable", "notes": []}

    monkeypatch.setattr(context, "retrieve_knowledge", unavailable)
    fresh = asyncio.run(context.read_context(receipt_id, None, 5, actor="operator"))
    assert fresh["current"]["task"]["state"] == "queued"
    assert fresh["current"]["queue"]["position"] == 1
    assert fresh["decision"]["open"] is True
    assert fresh["decision"]["chat"][-1]["note"] == (
        "Which retention scope is intended?"
    )
    assert [item["message_id"] for item in fresh["recent_exchanges"]] == [
        "factory-brief:test-task",
        "factory-request:operator:question-1",
    ]


def test_fresh_context_current_records_outweigh_stale_knowledge(
    db, github, monkeypatch
):
    import asyncio
    from factory.orchestration import conductor_context as context

    configure()
    receipt_id = escalate(db, "close")
    with Session(db) as session:
        control = session.get(FactoryControl, "factory")
        control.state = "stopped"
        row = session.get(FactoryReceipt, receipt_id)
        row.state = "succeeded"
        session.add(control)
        session.add(row)
        session.commit()

    async def stale(*_args):
        return {
            "status": "available",
            "authority": "untrusted_context_only",
            "notes": [
                {
                    "note_id": "old-authority",
                    "verification_state": "invalidated",
                    "snippet": "The task may continue.",
                }
            ],
        }

    monkeypatch.setattr(context, "retrieve_knowledge", stale)
    fresh = asyncio.run(context.read_context(receipt_id, None, 5, actor="operator"))
    assert fresh["current"]["control"]["state"] == "stopped"
    assert fresh["current"]["task"]["state"] == "succeeded"
    assert fresh["knowledge"]["notes"][0]["verification_state"] == "invalidated"


def test_receipt_knowledge_partitions_private_operator_exchanges(
    db, github, monkeypatch
):
    from types import SimpleNamespace
    from factory.orchestration import conductor_context as context

    receipt_id = escalate(db, "close")
    configure()
    decisions.request_decision(
        receipt_id,
        current_decision_id(),
        "chat",
        "operator-a",
        request_key="same-key",
        note="Operator A private constraint.",
        action="chat",
    )
    decisions.request_decision(
        receipt_id,
        current_decision_id(),
        "chat",
        "operator-b",
        request_key="same-key",
        note="Operator B private constraint.",
        action="chat",
    )
    recorded = []
    seen = set()

    def ingest(session, **kwargs):
        recorded.append(kwargs)
        created = kwargs["content"] not in seen
        seen.add(kwargs["content"])
        return SimpleNamespace(raw_id=f"raw-{len(seen)}"), created

    monkeypatch.setattr("core.db.get_engine", lambda: db)
    monkeypatch.setattr("knowledge.api.ingest_raw_with_status", ingest)
    result = context.maintain_receipt_knowledge(receipt_id)
    replay = context.maintain_receipt_knowledge(receipt_id)
    by_scope = {item["extra"]["scope"]: item["content"] for item in recorded}

    shared = f"session:factory-receipt:{receipt_id}"
    operator_a = f"personal:operator-a:factory-receipt:{receipt_id}"
    operator_b = f"personal:operator-b:factory-receipt:{receipt_id}"
    assert result["status"] == "queued"
    assert replay["status"] == "duplicate"
    assert set(by_scope) == {shared, operator_a, operator_b}
    assert "Operator A private constraint." not in by_scope[shared]
    assert "Operator B private constraint." not in by_scope[shared]
    assert "Operator A private constraint." in by_scope[operator_a]
    assert "Operator B private constraint." not in by_scope[operator_a]
    assert "Operator B private constraint." in by_scope[operator_b]
    assert "Operator A private constraint." not in by_scope[operator_b]
    assert "factory-request:operator-a:same-key" in by_scope[operator_a]
    assert "factory-request:operator-b:same-key" in by_scope[operator_b]
    assert all(item["extra"]["factory_receipt_generation"] == 0 for item in recorded)


def test_interrupted_knowledge_maintenance_retries_without_replaying_decision(
    db, github, monkeypatch
):
    from types import SimpleNamespace
    from factory.orchestration import conductor_context as context

    receipt_id = escalate(db, "close")
    identity = current_decision_id()
    completed = durable_answer(receipt_id, identity)
    attempts = []

    def ingest(session, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("knowledge store unavailable")
        return SimpleNamespace(raw_id="raw-recovered"), True

    monkeypatch.setattr("core.db.get_engine", lambda: db)
    monkeypatch.setattr("knowledge.api.ingest_raw_with_status", ingest)
    with pytest.raises(RuntimeError, match="unavailable"):
        context.maintain_request_knowledge("operator", "request-1")
    recovered = context.maintain_request_knowledge("operator", "request-1")
    assert recovered["status"] == "queued"
    assert durable_answer(receipt_id, identity) == completed
    assert len(github.comments) == 1


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
    assert durable_chat(db, receipt_id, "Which tier?")["requeued"]
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
    result = durable_chat(db, receipt_id, "Which tier?")
    assert result["requeued"] is False
    assert "neither in the policy allowlist" in result["blocked_by"]
    assert "Operator asks: Which tier?" in github.bodies()
    with Session(db) as session:
        assert session.get(FactoryReceipt, receipt_id).state == "succeeded"
    view = controls.escalations(controls.status()["receipts"])[0]
    assert view["chat"][0]["requeued"] is False
    assert "neither in the policy allowlist" in view["chat"][0]["blocked_by"]


def test_rebrief_of_previous_batch_cannot_poison_next_batch_queue(db, github):
    configure(issues=(5,))
    receipt_id = escalate(db)
    with Session(db) as session:
        control = session.get(FactoryControl, "factory")
        policy = json.loads(control.policy_json)
        policy["intake"].update(refine_enabled=True, refinement_batch=[9])
        control.policy_json = json.dumps(policy)
        session.add(control)
        session.commit()
        assert not controls.admission_eligible(
            policy, ISSUE, "factory:intake", task_class="refine", repo=REPO
        )
    result = durable_chat(db, receipt_id, "Revisit the earlier brief")
    assert result["requeued"] is False
    assert "admission will not select it" in result["blocked_by"]
    with Session(db) as session:
        assert session.get(FactoryReceipt, receipt_id).state == "succeeded"
        assert (
            session.exec(
                select(FactoryReceipt).where(FactoryReceipt.state == "queued")
            ).all()
            == []
        )


def test_a_generation_the_policy_has_moved_past_blocks_the_re_brief(db, github):
    configure(generation=4)
    receipt_id = escalate(db)
    result = durable_chat(db, receipt_id, "Which tier?")
    assert result["state"] == "refused"
    assert result["status"] == 409
    assert "receipt generation 0" in result["reason"]
    assert "policy is on generation 4" in result["reason"]
    assert "no issue changes were made" in result["reason"]
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

    result = durable_chat(db, receipt_id, "Which tier?")

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
    refused = durable_chat(db, receipt_id, "Which tier?")
    assert refused["state"] == "refused"
    assert refused["status"] == 409


def test_a_dismiss_repeated_returns_the_first_one_rather_than_a_second(db, github):
    receipt_id = escalate(db, "split")
    first = decisions.apply_decision(receipt_id, "escape:dismiss", "joe@example.test")
    second = decisions.apply_decision(receipt_id, "escape:dismiss", "joe@example.test")
    assert first["applied"] is True
    assert second["applied"] is False
    assert second["resolution"]["decided_at"] == first["resolution"]["decided_at"]
    assert audit_actions(db).count("decision_applied") == 1


def envelope_task(*, max_turns=6, max_attempts=1, binding=True):
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
    if binding:
        policy["task_budget_binding"] = True
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
        "assessment": ASSESSMENT,
        **changes,
    }


def settle_funding_request(task, request, decision):
    run = {
        "id": 900,
        "node_key": request["node_key"],
        "attempt": 1,
        "status": "succeeded",
        "pin": {"model": request["model"]},
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
        "_submission_projection",
        lambda *_args, **_kwargs: conductor.DecisionProjection(
            revision=0,
            edits=[],
            prepared=[],
            refusal={
                "code": "envelope_exceeded",
                "detail": "envelope exceeded: " + json.dumps(deficit),
            },
        ),
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


@pytest.mark.parametrize(
    "status,outcome,category,reason",
    [
        (
            "failed",
            {
                "invocation_phase": "not_invoked",
                "reason": "Executor cancelled before invoking the guest",
            },
            "pre_model_failure",
            "Executor cancelled",
        ),
        (
            "failed",
            {"reason": "guest_cessation_confirmed"},
            "execution_failed",
            "guest_cessation_confirmed",
        ),
        (
            "succeeded",
            {"value": {"reason": "Malformed decision"}},
            "artifact_invalid",
            "'action' is a required property",
        ),
    ],
)
def test_funding_failure_categories(db, monkeypatch, status, outcome, category, reason):
    task, _policy = envelope_task()
    add_spent_turns(db, task)
    monkeypatch.setenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "true")
    monkeypatch.setattr(
        funding, "_issue", lambda _task: {"number": ISSUE, "state": "open"}
    )
    assert funding.request(task, "Review remaining work")
    with controls._read_session() as session:
        request = funding.pending(session, task["id"])
    run = {
        "id": 900,
        "node_key": request["node_key"],
        "attempt": 1,
        "status": status,
        "pin": {"model": request["model"]},
        "dispatch_key": request["start_key"],
        "outcome_json": json.dumps(outcome),
    }
    funding.settle(task, run, request)
    with controls._read_session() as session:
        settled = funding.latest(session, task["id"], "funding_review_settled")
        assert funding.amendment(session, task["id"]) is None
    assert settled["failure_category"] == category
    assert reason in settled["refusal"]
    assert settled["execution_reason"] == outcome.get("reason")
    assert settled["accounting_basis"] == conductor.graph.settled_zero_basis(outcome)


def test_unknown_funding_invocation_stays_pending(db, monkeypatch):
    task, _policy = envelope_task()
    add_spent_turns(db, task)
    monkeypatch.setenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "true")
    monkeypatch.setattr(
        funding, "_issue", lambda _task: {"number": ISSUE, "state": "open"}
    )
    assert funding.request(task, "Review remaining work")
    with controls._read_session() as session:
        request = funding.pending(session, task["id"])
    with Session(db) as session:
        session.add(
            FactoryStart(
                task_id=task["id"],
                start_key=request["start_key"],
                actor="executor",
                model=request["model"],
                max_cost_usd=1,
                status="uncertain",
            )
        )
        session.commit()
    funding.settle(task, {"status": "uncertain", "outcome_json": "{}"}, request)
    with controls._read_session() as session:
        assert funding.pending(session, task["id"])["audit_id"] == request["audit_id"]
        assert funding.latest(session, task["id"], "funding_review_settled") is None
        start = session.exec(
            select(FactoryStart).where(FactoryStart.start_key == request["start_key"])
        ).one()
        assert start.status == "uncertain" and start.accounting_basis is None
        assert start.cost_usd is None


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


def test_deficit_past_the_old_objective_ceiling_requires_budget_approval(
    db, monkeypatch
):
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
            "ceiling_usd": 200.0,
        },
    )
    assert funding.request(task, "Fund the recorded deficit")
    with controls._read_session() as session:
        request = funding.pending(session, task["id"])
    # A valid proposal has no fixed $200 cap, but a higher task budget needs
    # operator approval rather than an automatic grant.
    settle_funding_request(task, request, funding_decision(task_budget_usd=195.0))
    with controls._read_session() as session:
        grant = funding.amendment(session, task["id"])
        settled = funding.latest(session, task["id"], "funding_review_settled")
    assert settled["refusal"] is None
    assert grant is None
    assert settled["budget_approval_required"] == {
        "approved_task_budget_usd": 30.0,
        "proposed_task_budget_usd": 195.0,
    }
    assert controls.task_snapshot(task["id"])["task_paused"]


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


MERIT = {
    "progress": {"commits_pushed": 2, "reviews_approved": 0},
    "proximity": {"pull_request": 6546, "checks": "success"},
    "value": {"labels": ["bug"], "value_labels": ["bug"]},
}
ASSESSMENT = {
    "progress": "Two commits and an open PR with green checks.",
    "proximity": "Only the independent review is left.",
    "value": "A real bug on the delivery lane.",
}


def _merit_judge(monkeypatch):
    from factory.orchestration import factory_judge

    monkeypatch.setenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "true")
    monkeypatch.setattr(factory_judge, "merit_evidence", lambda _task, _runs: MERIT)
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


def _repeated_deficit(monkeypatch, task, policy, deficit):
    """Two matching envelope refusals; return any cards raised."""
    _merit_judge(monkeypatch)
    monkeypatch.setattr(
        conductor,
        "_submission_projection",
        lambda *_args, **_kwargs: conductor.DecisionProjection(
            revision=0,
            edits=[],
            prepared=[],
            refusal={
                "code": "envelope_exceeded",
                "detail": "envelope exceeded: " + json.dumps(deficit),
            },
        ),
    )
    escalations = []
    monkeypatch.setattr(
        conductor,
        "_escalate_task",
        lambda _task, decision, _cause, _runs: escalations.append(decision),
    )
    decision = plan_for_five_more()
    conductor._apply_decision(task, policy, decision, "decision:1", [])
    conductor._apply_decision(task, policy, decision, "decision:2", [])
    return escalations


def _pending(task):
    with controls._read_session() as session:
        return funding.pending(session, task["id"])


def _grant(task):
    with controls._read_session() as session:
        return funding.amendment(session, task["id"])


def _settled(task):
    with controls._read_session() as session:
        return funding.latest(session, task["id"], "funding_review_settled")


@pytest.mark.parametrize("answer", ["resume", "amend", "amend_and_resume"])
@pytest.mark.parametrize("verdict", ["escalate", "budget_approval"])
def test_operator_answer_rearms_a_full_progress_step(db, monkeypatch, answer, verdict):
    from factory.orchestration import factory_progress_watchdog as watchdog
    from factory.orchestration import factory_policy_amendment as amendments

    task, policy = envelope_task(max_turns=20)
    add_spent_turns(db, task, count=8)
    _merit_judge(monkeypatch)
    cards = []
    monkeypatch.setattr(
        conductor, "_escalate_funding_doubt", lambda *args: cards.append(args)
    )
    assert watchdog.check(task["id"], policy) == "funding_review"
    request = _pending(task)
    settle_funding_request(
        task,
        request,
        funding_decision(
            action="escalate" if verdict == "escalate" else "continue",
            task_budget_usd=40 if verdict == "budget_approval" else 30,
            assessment=ASSESSMENT,
        ),
    )
    assert _settled(task)["refusal"] is None
    assert bool(cards) == (verdict == "escalate")
    # A settlement awaiting its card must block a second trigger even before
    # the card writer pauses the receipt.
    monkeypatch.setattr(
        funding, "_issue", lambda _: pytest.fail("operator answer was re-litigated")
    )
    assert funding.request(task, "Progress crossing", progress=True)
    assert _pending(task) is None
    assert controls.set_control("pause_task", "factory:funding", task_id=task["id"])[
        "ok"
    ]
    if "amend" in answer:
        with Session(db) as session:
            receipt_id = controls._receipt(session, task["id"]).id
        proposal = amendments.preview(
            receipt_id, {"task_budget_usd": 40}, "Fund the correction."
        )
        assert amendments.apply(
            receipt_id,
            {"task_budget_usd": 40},
            "Fund the correction.",
            actor="operator",
            request_key="fund-correction",
            expected_preview_sha256=proposal["preview_sha256"],
        )["ok"]
    if "resume" in answer:
        assert controls.set_control("resume_task", "operator", task_id=task["id"])["ok"]
    assert watchdog.check(task["id"], policy) == "under_threshold"
    with controls._read_session() as session:
        detail = watchdog.detail(session, task["id"], policy)
        assert detail["next_threshold_turns"] == 16
        assert (
            funding.latest(session, task["id"], "funding_review_requested")["audit_id"]
            == request["audit_id"]
        )
        assert not funding._awaiting_operator(session, task["id"], _settled(task))
    assert (
        funding.reconcile(
            task, policy, conductor.graph.node_runs(task["id"]), {"ok": True}
        )
        is False
    )
    assert len(cards) == (verdict == "escalate")
    if "resume" in answer:
        _merit_judge(monkeypatch)
        for index in range(8):
            with controls._locked_session() as (session, _control):
                session.add(
                    FactoryStart(
                        task_id=task["id"],
                        start_key=f"after-answer:{index}",
                        actor="test",
                        model="luna",
                        max_cost_usd=1,
                        status="succeeded",
                        cost_usd=0.1,
                    )
                )
            assert watchdog.check(task["id"], policy) == (
                "funding_review" if index == 7 else "under_threshold"
            )
        assert _pending(task)["audit_id"] > request["audit_id"]


def test_a_repeated_deficit_goes_to_the_merit_judge_not_a_person(db, monkeypatch):
    # #6541: $1.20 over a $50 envelope paged a person although the funding
    # judge was on, because only a planner-emitted request_funding reached it.
    task, policy = envelope_task()
    deficit = {
        "spare_turns": 2,
        "spare_usd": 1.0,
        "turns": {"allowed": 6, "needed": 6},
        "usd": {"allowed": 30.0, "needed": 31.2},
    }
    assert _repeated_deficit(monkeypatch, task, policy, deficit) == []
    request = _pending(task)
    # Pinned $30 plus min(1 x $30, $50): the hard ceiling is $60.
    assert request["merit"]["ceiling"]["ceiling_usd"] == 60.0
    assert request["merit"]["ceiling"]["turn_ceiling"] == 12
    assert request["merit"]["evidence"] == MERIT
    (node,) = [
        n
        for n in conductor.graph.load_graph(task["id"])
        if n["node_key"] == request["node_key"]
    ]
    assert "Verified progress" in node["prompt"]
    assert "money already spent is a reason to finish" in node["prompt"]
    assert '"pull_request": 6546' in node["prompt"]

    # The judge may propose more than the old fixed ceiling. That valid
    # assessment is recorded, but it cannot approve its own budget increase.
    settle_funding_request(
        task,
        request,
        funding_decision(task_budget_usd=95.0, assessment=ASSESSMENT),
    )
    grant = _grant(task)
    assert grant is None
    assert _settled(task)["budget_approval_required"] == {
        "approved_task_budget_usd": 30.0,
        "proposed_task_budget_usd": 95.0,
    }
    assert _settled(task)["assessment"] == ASSESSMENT
    assert controls.task_snapshot(task["id"])["task_paused"]


def test_a_judge_figure_below_the_deficit_still_needs_budget_approval(db, monkeypatch):
    task, policy = envelope_task()
    deficit = {"usd": {"allowed": 30.0, "needed": 31.2}}
    assert _repeated_deficit(monkeypatch, task, policy, deficit) == []
    settle_funding_request(
        task,
        _pending(task),
        funding_decision(task_budget_usd=30.0, assessment=ASSESSMENT),
    )
    assert _grant(task) is None
    assert _settled(task)["budget_approval_required"] == {
        "approved_task_budget_usd": 30.0,
        "proposed_task_budget_usd": 31.2,
    }
    assert controls.task_snapshot(task["id"])["task_paused"]


def test_a_deficit_past_the_turn_ceiling_asks_a_person(db, monkeypatch):
    task, policy = envelope_task()
    deficit = {"turns": {"allowed": 6, "needed": 13}}
    escalations = _repeated_deficit(monkeypatch, task, policy, deficit)
    assert len(escalations) == 1
    assert "Should the exact deficit be funded" in escalations[0]["question"]
    assert _pending(task) is None
    with controls._read_session() as session:
        declined = funding.latest(session, task["id"], "merit_funding_declined")
    assert declined["refusal"] == "turns_over_ceiling"
    assert declined["ceiling"]["turn_ceiling"] == 12


def test_a_dollar_deficit_past_the_old_ceiling_goes_to_the_judge(db, monkeypatch):
    # Only turns bound a merit grant; a dollar deficit is recorded (#7161).
    task, policy = envelope_task()
    deficit = {"usd": {"allowed": 30.0, "needed": 61.0}}
    assert _repeated_deficit(monkeypatch, task, policy, deficit) == []
    request = _pending(task)
    assert request["merit"]["ceiling"]["ceiling_usd"] == 60.0
    with controls._read_session() as session:
        assert funding.latest(session, task["id"], "merit_funding_declined") is None
    settle_funding_request(
        task, request, funding_decision(task_budget_usd=30.0, assessment=ASSESSMENT)
    )
    assert _grant(task) is None
    assert (
        _settled(task)["budget_approval_required"]["proposed_task_budget_usd"] == 61.0
    )
    assert controls.task_snapshot(task["id"])["task_paused"]


def test_a_judge_that_doubts_the_work_escalates_to_a_person(db, monkeypatch):
    task, policy = envelope_task()
    deficit = {"usd": {"allowed": 30.0, "needed": 31.2}}
    escalations = _repeated_deficit(monkeypatch, task, policy, deficit)
    assert escalations == []
    settle_funding_request(
        task,
        _pending(task),
        funding_decision(
            action="escalate",
            reason="The same correction failed three times on one step.",
            task_budget_usd=0,
            additional_work_turns=0,
            lease_minutes=0,
            assessment={**ASSESSMENT, "doubts": ["repeated failure on one step"]},
        ),
    )
    assert _grant(task) is None
    settled = _settled(task)
    assert settled["refusal"] is None
    assert "doubts this task" in settled["escalated"]
    (card,) = escalations
    assert "same correction failed" in card["question"]
    assert card["options"][0]["key"] == "fund-and-resume"


def test_a_grant_the_hard_ceiling_cannot_hold_escalates(db, monkeypatch):
    monkeypatch.setenv("FACTORY_MERIT_JUDGE_MAX_EXTENSION_MULTIPLE", "0")
    task, policy = envelope_task()
    add_spent_turns(db, task, count=8)
    deficit = {"usd": {"allowed": 30.0, "needed": 29.0}}
    escalations = _repeated_deficit(monkeypatch, task, policy, deficit)
    assert escalations == []
    request = _pending(task)
    assert request["merit"]["ceiling"]["turn_ceiling"] == 8
    settle_funding_request(task, request, funding_decision(assessment=ASSESSMENT))
    assert _grant(task) is None
    assert "policy ceiling" in _settled(task)["escalated"]
    assert len(escalations) == 1


def test_a_merit_verdict_without_its_assessment_is_refused(db, monkeypatch):
    task, policy = envelope_task()
    deficit = {"usd": {"allowed": 30.0, "needed": 31.2}}
    assert _repeated_deficit(monkeypatch, task, policy, deficit) == []
    decision = funding_decision()
    decision.pop("assessment")
    settle_funding_request(task, _pending(task), decision)
    assert _grant(task) is None
    assert _settled(task)["refusal"] == "a merit verdict needs its assessment"


def test_escalate_is_refused_on_a_historical_review_without_merit(db, monkeypatch):
    task, _policy = envelope_task()
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
    assert funding.request(task, "Assess value")
    # Simulate a durable request recorded before merit enrichment existed.
    with controls._locked_session() as (session, _control):
        request = _pending(task)
        audit = session.get(FactoryAudit, request["audit_id"])
        payload = json.loads(audit.detail_json)
        payload.pop("merit")
        audit.detail_json = json.dumps(payload)
    settle_funding_request(
        task,
        _pending(task),
        funding_decision(
            action="escalate",
            task_budget_usd=0,
            additional_work_turns=0,
            lease_minutes=0,
        ),
    )
    assert _settled(task)["refusal"] == "escalate is honoured only on a merit review"


def test_merit_judging_enriches_repeated_deficits_without_a_rollout_flag(
    db, monkeypatch
):
    from factory.orchestration import factory_judge

    task, policy = envelope_task()
    assert _repeated_deficit(monkeypatch, task, policy, {"usd": {"needed": 30.5}}) == []
    request = _pending(task)
    assert request["merit"] == {
        "ceiling": factory_judge.ceiling(policy),
        "evidence": MERIT,
    }
    node = next(
        node
        for node in conductor.graph.load_graph(task["id"])
        if node["node_key"] == request["node_key"]
    )
    assert factory_judge.CRITERIA in node["prompt"]
    assert json.dumps(MERIT) in node["prompt"]


def test_missing_pinned_policy_cannot_judge_a_deficit(db, monkeypatch):
    task, _policy = envelope_task()
    _merit_judge(monkeypatch)
    with controls._locked_session() as (session, _control):
        controls._receipt(session, task["id"]).policy_json = None
    assert funding._pinned_ceiling(task["id"]) is None
    assert not funding.judge_deficit(task, {"usd": {"needed": 30.5}}, "r")
    assert _pending(task) is None


def test_request_without_a_bound_retains_legacy_enrichment_fallback(db, monkeypatch):
    task, _policy = envelope_task()
    _merit_judge(monkeypatch)
    monkeypatch.setattr(funding, "_pinned_ceiling", lambda _: None)
    monkeypatch.setattr(
        funding.factory_judge,
        "merit_evidence",
        lambda *_: pytest.fail("missing policy must not collect merit evidence"),
    )
    assert funding.request(task, "Legacy request without pinned policy")
    assert "merit" not in _pending(task)


def test_unreadable_deficit_is_refused_without_starting_a_review(db, monkeypatch):
    task, _policy = envelope_task()
    _merit_judge(monkeypatch)
    assert not funding.judge_deficit(task, {"spare_usd": 1}, "r")
    assert _pending(task) is None
    with controls._read_session() as session:
        assert (
            funding.latest(session, task["id"], "merit_funding_declined")["refusal"]
            == "deficit_unreadable"
        )


def test_funding_disabled_unenrolled_task_cannot_judge_a_deficit(db, monkeypatch):
    task, _policy = envelope_task()
    monkeypatch.setenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "false")
    assert not funding.judge_deficit(task, {"usd": {"needed": 30.5}}, "r")
    assert _pending(task) is None


def test_the_merit_ceiling_takes_the_smaller_of_multiple_and_cap(monkeypatch):
    from factory.orchestration import factory_judge

    monkeypatch.setenv("FACTORY_MERIT_JUDGE_MAX_EXTENSION_USD", "40")
    bound = factory_judge.ceiling({"task_budget_usd": 200.0, "max_task_turns_hard": 9})
    assert bound["max_extension_usd"] == 40.0 and bound["ceiling_usd"] == 240.0
    assert bound["turn_ceiling"] == 18
    small = factory_judge.ceiling({"task_budget_usd": 20.0, "max_turns_per_task": 4})
    assert small["ceiling_usd"] == 40.0 and small["turn_ceiling"] == 8
    refusal = factory_judge.deficit_refusal
    # A dollar deficit alone is judgeable: only turns bound a grant (#7161).
    assert refusal({"usd": {"needed": 241}}, bound) is None
    assert refusal({"turns": {"needed": 19}}, bound) == "turns_over_ceiling"
    assert refusal({"spare_usd": 1}, bound) == "deficit_unreadable"
    assert refusal({"usd": {"needed": 239.9}, "turns": {"needed": 18}}, bound) is None


def test_merit_evidence_reads_progress_proximity_and_value(monkeypatch):
    from factory.orchestration import factory_judge

    head = "a" * 40
    reads = {
        "pulls/6546": {
            "state": "open",
            "draft": False,
            "mergeable_state": "clean",
            "head": {"sha": head},
        },
        f"commits/{head}/status": {"state": "success"},
        f"issues/{ISSUE}": {
            "title": "factory: fix conductor livelock",
            "labels": [{"name": "bug"}, {"name": "roadmap"}],
            "milestone": {"title": "Factory MVP"},
        },
    }
    monkeypatch.setattr(conductor, "github_get", lambda _repo, suffix: reads[suffix])
    monkeypatch.setattr(factory_judge, "_blocks_open", lambda _task: 2)
    monkeypatch.setenv("FACTORY_MERIT_JUDGE_PRIORITY_ISSUES", f"6463, #{ISSUE}")

    def run(ident, key, status, **artifact):
        return {
            "id": ident,
            "node_key": key,
            "status": status,
            "outcome_json": json.dumps({"value": artifact}),
        }

    runs = [
        run(1, "implement_fix", "failed"),
        run(2, "implement_fix", "succeeded", head_sha=head, pr_number=6546),
        run(3, "review_fix", "succeeded", verdict="changes_requested"),
        run(4, "correct_fix", "succeeded", head_sha=head),
        run(5, "review_fix_2", "succeeded", verdict="approve"),
    ]
    found = factory_judge.merit_evidence(
        {"id": "t", "repo": REPO, "issue_number": ISSUE}, runs
    )
    assert found["progress"]["commits_pushed"] == 1
    assert found["progress"]["implementation_succeeded"] == 2
    assert found["progress"]["review_verdicts"] == ["changes_requested", "approve"]
    assert found["progress"]["reviews_approved"] == 1
    assert found["proximity"] == {
        "pull_request": 6546,
        "state": "open",
        "merged": False,
        "draft": False,
        "mergeable_state": "clean",
        "head": head[:12],
        "checks": "success",
    }
    assert found["value"]["value_labels"] == ["bug", "roadmap"]
    assert found["value"]["named_priority"] is True
    assert found["value"]["factory_work"] is True
    assert found["value"]["milestone"] == "Factory MVP"
    assert found["value"]["blocks_open_work"] == 2


def test_merit_evidence_states_a_failed_read_instead_of_raising(monkeypatch):
    from factory.orchestration import factory_judge

    def broken(_repo, _suffix):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(conductor, "github_get", broken)
    monkeypatch.setattr(factory_judge, "_blocks_open", lambda _task: 0)
    found = factory_judge.merit_evidence(
        {"id": "t", "repo": REPO, "issue_number": ISSUE, "delivery_pr_number": 9},
        [],
    )
    assert found["proximity"]["unavailable"] == "ConnectError"
    assert found["value"]["unavailable"] == "ConnectError"


def _split_children(db, receipt_id, children):
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        document = json.loads(row.escalation_json)
        document["options"][0]["detail"]["children"] = children
        row.escalation_json = json.dumps(document)
        session.add(row)
        session.commit()


SPLIT_CHILDREN = [
    {"title": "Validate the flip on hub", "body": "b", "operational": True},
    {
        "title": "factory: live-validate bound zero-turn settlement before enabling it",
        "body": "b",
    },
    {"title": "The API", "body": "The decision endpoint."},
]


def test_an_operational_split_child_opens_with_needs_human(db, github, monkeypatch):
    # #6559 (receipt 724): a live-check child opened without an exclusion
    # label and intake briefed it within seconds.
    receipt_id = escalate(db, "split")
    _split_children(db, receipt_id, SPLIT_CHILDREN)
    decisions.apply_decision(receipt_id, "split", "joe@example.test")
    created = [write[2] for write in github.writes if write[1] == "issues"]
    assert [child.get("labels") for child in created] == [
        ["needs-human"],
        ["needs-human"],
        None,
    ]


def test_the_option_schema_accepts_an_operational_child():
    option = options("split")[0]
    option["detail"]["children"][0]["operational"] = True
    assert (
        controls.verify_option_list([option, options("split")[1]], subject="x") is None
    )


def _delivered_pause(**gate):
    return {
        "action": "pause",
        "reason": "Delivered versus requested: the repository scope is on main.",
        "question": "Only live checks remain. Enable the flag or leave the lane?",
        "options": options("deliver")[:2],
        "gate": {
            "kind": "repository_delivered",
            "classification": "reversible",
            "effects": {
                "spends_money": False,
                "deletes_data": False,
                "touches_external_account": False,
                "uses_credentials": False,
            },
            "reason": "PR #6334 merged the repository scope default-off.",
            "live_checks": ["Enable the proof on the hub", "Watch one settlement"],
            "delivered_prs": [6334],
            **gate,
        },
    }


def _merged_pulls(monkeypatch, github, pulls):
    def get(repo, suffix):
        if suffix.startswith("pulls/"):
            return pulls[suffix]
        return github.get(repo, suffix)

    monkeypatch.setattr(conductor, "github_get", get)


MERGED_6334 = {
    "pulls/6334": {
        "merged": True,
        "merged_at": "2026-09-28T00:00:00Z",
        "title": "feat(factory): bound zero-turn settlement",
        "body": f"Refs #{ISSUE}\n\nConductor rescope: default off.",
        "base": {"repo": {"full_name": REPO}},
    }
}


def _handoff_task(db, monkeypatch, github):
    task, _policy = envelope_task()
    github.labels = {"agent-ready"}
    _merged_pulls(monkeypatch, github, MERGED_6334)
    return task


def test_only_live_checks_left_hands_off_without_a_card(db, github, monkeypatch):
    # #6288 and six others on 2026-09-30: the planner found the repository
    # work merged and paged a person about the live checks.
    task = _handoff_task(db, monkeypatch, github)
    conductor._escalate_task(task, _delivered_pause(), "factory-decision:c_1:1", [])

    (child,) = [write[2] for write in github.writes if write[1] == "issues"]
    assert child["labels"] == ["needs-human"]
    assert "- [ ] Enable the proof on the hub" in child["body"]
    assert f"Live checks for #{ISSUE}" in child["title"]
    assert "agent-ready" not in github.labels
    assert "needs-human" not in github.labels
    assert github.state == "open"
    (comment,) = github.comments
    assert "#101" in comment["body"] and "#6334" in comment["body"]
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        assert row.state == "cancelled"
        assert row.escalation_json is None
        recorded = session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "repository_scope_delivered"
            )
        ).one()
    detail = json.loads(recorded.detail_json)
    assert detail["delivered_prs"] == [6334] and detail["child_number"] == 101
    assert "task_needs_person_notified" not in audit_actions(db)
    assert "conductor_escalated" not in audit_actions(db)


def test_checks_tracked_elsewhere_close_the_issue_as_delivered(db, github, monkeypatch):
    task = _handoff_task(db, monkeypatch, github)
    conductor._escalate_task(
        task, _delivered_pause(tracked_in=5706), "factory-decision:c_1:1", []
    )
    assert [write for write in github.writes if write[1] == "issues"] == []
    assert github.state == "closed"
    assert (
        "PATCH",
        f"issues/{ISSUE}",
        {"state": "closed", "state_reason": "completed"},
    ) in github.writes
    assert "#5706" in github.comments[0]["body"]


def test_an_unverified_delivery_keeps_the_decision_card(db, github, monkeypatch):
    task = _handoff_task(db, monkeypatch, github)
    _merged_pulls(
        monkeypatch,
        github,
        {
            "pulls/6334": {
                **MERGED_6334["pulls/6334"],
                "merged": False,
                "merged_at": None,
            }
        },
    )
    conductor._escalate_task(task, _delivered_pause(), "factory-decision:c_1:1", [])
    assert "needs-human" in github.labels
    assert "operational_handoff_refused" in audit_actions(db)
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        assert row.state == "escalated"


def test_the_issue_reference_must_be_this_issue():
    from factory.orchestration import factory_operational_handoff as handoff

    assert handoff.references_issue("Refs #7", REPO, 7)
    assert handoff.references_issue(f"Refs {REPO}#7", REPO, 7)
    assert handoff.references_issue(f"https://github.com/{REPO}/issues/7", REPO, 7)
    assert not handoff.references_issue("Refs #70", REPO, 7)
    assert not handoff.references_issue("Refs other/repo#7", REPO, 7)
    assert not handoff.references_issue("Refs ##7", REPO, 7)


def test_explicit_held_readmission_consumes_current_queue_authority(db, github):
    from factory.orchestration.factory_intake import (
        admit_next,
        operator_readmission_queued,
    )

    receipt_id = escalate(db, "deliver", state="escalated", task_class="bug-fix")
    configure(issues=())
    with Session(db) as session, session.begin():
        row = session.get(FactoryReceipt, receipt_id)
        row.direction_json = json.dumps({"landing_hold": {"reason": "leave unmerged"}})
    result = decisions.request_decision(
        receipt_id,
        current_decision_id(),
        "chat",
        "operator",
        request_key="continue-held",
        note="Continue under the existing hold",
        action="chat",
    )
    assert result["requeued"] is True
    with Session(db) as session:
        assert operator_readmission_queued(session, receipt_id)
    admitted = admit_next("scheduler")
    assert admitted["ok"] and admitted["receipt_id"] == receipt_id
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        assert json.loads(row.direction_json)["landing_hold"] == {
            "reason": "leave unmerged"
        }
        assert not operator_readmission_queued(session, receipt_id)


@pytest.mark.parametrize(
    "conflict",
    [None, "conductor_model", "worker_model", "reviewer_model", "empty_pool"],
)
def test_model_restriction_survives_same_receipt_readmission(db, github, conflict):
    receipt_id = escalate(db, "deliver", state="escalated", task_class="bug-fix")
    configure(issues=())
    restriction = {
        "removed_models": ["spark"],
        "actor": "operator",
        "request_key": "remove-spark",
    }
    with Session(db) as session, session.begin():
        row = session.get(FactoryReceipt, receipt_id)
        row.direction_json = json.dumps(
            {
                "landing_hold": {"reason": "leave unmerged"},
                "model_restriction": restriction,
            }
        )
        policy = json.loads(session.get(FactoryControl, "factory").policy_json)
        policy["allowed_models"].append("spark")
        policy["model_pools"] = {
            "implement": ["spark", "opus"],
            "worker": ["luna", "spark"],
        }
        if conflict == "empty_pool":
            policy["model_pools"]["implement"] = ["spark"]
        elif conflict:
            policy[conflict] = "spark"
            if conflict == "worker_model":
                policy["model_pools"]["worker"] = ["spark", "luna"]
        history = [task.model_dump() for task in session.exec(select(SwarmTask)).all()]
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    result = decisions.request_decision(
        receipt_id,
        current_decision_id(),
        "chat",
        "operator",
        request_key="continue-restricted",
        note="Continue with existing restrictions",
        action="chat",
    )
    assert result["requeued"] is True
    with Session(db) as session:
        row = session.get(FactoryReceipt, receipt_id)
        assert json.loads(row.direction_json)["model_restriction"] == restriction
        assert row.task_id is None and row.policy_json is None
    admitted = admit_next("scheduler")
    with Session(db) as session:
        assert [
            session.get(SwarmTask, task["id"]).model_dump() for task in history
        ] == history
        row = session.get(FactoryReceipt, receipt_id)
        assert json.loads(row.direction_json)["model_restriction"] == restriction
        if conflict:
            assert admitted["reason"] == "model_restriction_invalid"
            assert not admitted["ok"]
            assert row.task_id is None and row.state == "queued"
            assert len(session.exec(select(SwarmTask)).all()) == len(history)
        else:
            assert admitted["ok"] and admitted["receipt_id"] == receipt_id
            effective = controls._effective_policy(session, row)
            assert "spark" not in effective["allowed_models"]
            assert effective["model_pools"] == {
                "implement": ["opus"],
                "worker": ["luna"],
            }
            assert "spark" in json.loads(row.policy_json)["allowed_models"]


def test_judge_separates_transport_missing_output_and_code_failure():
    from factory.orchestration.factory_judge import failure_kind, progress

    def run(ident, status, outcome):
        return {
            "id": ident,
            "node_key": "review_1",
            "status": status,
            "outcome_json": json.dumps(outcome),
        }

    runs = [
        run(1, "failed", {"invocation_phase": "lost_before_guest"}),
        run(2, "uncertain", {"invocation_phase": "not_invoked"}),
        run(3, "failed", {}),
        run(4, "failed", {"artifact": {"status": "missing"}}),
    ]
    assert [failure_kind(row) for row in runs] == [
        "transport_before_model",
        "unknown_execution",
        "missing_output",
        "missing_or_invalid_artifact",
    ]
    assert progress(runs)["most_repeated_failure"] is None
    runs.append(run(5, "failed", {"error": "tests failed"}))
    assert progress(runs)["most_repeated_failure"] == {
        "node_key": "review_1",
        "failures": 1,
    }
    assert progress(runs)["failure_causes"]["unknown_execution"] == 1


# Routine planner pauses: the funding judge answers on the same task.


def routine_pause(**changes):
    return {
        "action": "pause",
        "reason": "The fix and its tests pass; carrying on is the natural next step.",
        "question": "Carry on with the working fix, or wait for direction?",
        "options": [
            {
                "key": "carry-on",
                "label": "Carry on with the working fix",
                "effect": "agent-ready",
                "detail": {"scope": "Finish the same fix and request review."},
            },
            {"key": "hold", "label": "Wait for direction", "effect": "hold"},
        ],
        **changes,
    }


@pytest.fixture
def routine(db, monkeypatch):
    task, policy = envelope_task(max_turns=20)
    _merit_judge(monkeypatch)
    monkeypatch.setenv(funding.ROUTINE_PAUSE_ENV, "true")
    _mirror_label(db, None)  # a synchronized work-item mirror, no labels
    state = {"labels": [], "state": "open", "cards": []}
    monkeypatch.setattr(
        conductor,
        "github_get",
        lambda _repo, _suffix: {
            "state": state["state"],
            "labels": [{"name": name} for name in state["labels"]],
        },
    )
    monkeypatch.setattr(
        conductor,
        "_escalate_task",
        lambda *args, **kwargs: state["cards"].append(args[1]["question"]),
    )
    return task, policy, state


def pause(task, policy, decision, ordinal=1):
    conductor._apply_decision(
        task, policy, decision, f"factory-decision:conductor_{ordinal}:1", []
    )


def audits_of(db, action):
    with Session(db) as session:
        return [
            json.loads(row.detail_json)
            for row in session.exec(
                select(FactoryAudit).where(FactoryAudit.action == action)
            )
        ]


def test_routine_pause_is_answered_by_the_judge_on_the_same_task(db, routine):
    task, policy, state = routine
    pause(task, policy, routine_pause())

    assert state["cards"] == []
    request = _pending(task)
    assert "recommends continuing" in request["trigger"]
    rejected = audits_of(db, "conductor_rejected")
    assert rejected[-1]["refusal_code"] == "routine_pause_funding_requested"
    assert conductor._decision_processed(task["id"], "factory-decision:conductor_1:1")

    settle_funding_request(task, request, funding_decision(assessment=ASSESSMENT))
    grant = _grant(task)
    assert grant is not None and grant["granted_work_turns"] >= 1
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        # Same receipt, same task: no re-admission and no reset lineage.
        assert row.task_id == task["id"] and row.state == "admitted"
        assert row.escalation_json is None


@pytest.mark.parametrize(
    ("change", "env"),
    [
        ({}, "false"),
        ({"gate": {"kind": "parameter"}}, "true"),
        (
            {"options": [routine_pause()["options"][1], routine_pause()["options"][0]]},
            "true",
        ),
        ({"question": "Rotate the deploy token before continuing?"}, "true"),
        ({"question": "Grant the agent access to the cluster?"}, "true"),
        ({"reason": "This would delete the old bucket."}, "true"),
        ({"question": "This is a security finding; continue?"}, "true"),
        ({"question": "   "}, "true"),
    ],
)
def test_non_routine_pauses_keep_their_card(db, routine, monkeypatch, change, env):
    task, policy, state = routine
    monkeypatch.setenv(funding.ROUTINE_PAUSE_ENV, env)
    pause(task, policy, routine_pause(**change))
    assert len(state["cards"]) == 1
    assert audits_of(db, "funding_review_requested") == []


@pytest.mark.parametrize(
    "label", ["human", "needs-human", "needs-thought", "security-finding"]
)
def test_held_issue_labels_keep_the_card(db, routine, label):
    task, policy, state = routine
    state["labels"] = [label]
    pause(task, policy, routine_pause())
    assert len(state["cards"]) == 1
    assert audits_of(db, "funding_review_requested") == []


@pytest.mark.parametrize(
    "direction",
    [
        {"nonrenewable_recovery": {"actor": "operator"}},
        {"landing_hold": {"actor": "op"}},
    ],
)
def test_shared_holds_keep_the_card_without_mutating_the_receipt(
    db, routine, direction
):
    task, policy, state = routine
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        row.direction_json = json.dumps(direction)
        session.add(row)
        session.commit()
    pause(task, policy, routine_pause())
    assert len(state["cards"]) == 1
    assert audits_of(db, "funding_review_requested") == []
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        assert json.loads(row.direction_json) == direction
        assert not row.task_paused


def test_recorded_human_handoff_keeps_the_card(db, routine):
    task, policy, state = routine
    with controls._locked_session() as (session, _control):
        controls._audit(session, "operator", "human_handoff", task_id=task["id"])
    pause(task, policy, routine_pause())
    assert len(state["cards"]) == 1


@pytest.mark.parametrize("control", ["pause_admissions", "stop"])
def test_paused_or_stopped_lane_never_routes_a_pause(db, routine, control):
    task, policy, state = routine
    assert controls.set_control(control, "operator")["ok"]
    pause(task, policy, routine_pause())
    assert audits_of(db, "funding_review_requested") == []
    assert len(state["cards"]) == 1


def test_repeat_without_new_work_is_one_repair_signal_and_a_card(db, routine):
    task, policy, state = routine
    pause(task, policy, routine_pause())
    settle_funding_request(
        task, _pending(task), funding_decision(assessment=ASSESSMENT)
    )
    assert _grant(task) is not None

    # The same question again, reworded, with no new artifact or head.
    pause(task, policy, routine_pause(question="Still carry on with the fix?"), 2)
    pause(task, policy, routine_pause(), 3)

    assert len(state["cards"]) == 2
    blockers = audits_of(db, funding.BLOCKER)
    assert len(blockers) == 1
    assert blockers[0]["reason"] == "funding_without_progress"
    assert blockers[0]["blocker_key"] == funding.blocker_key(
        REPO, ISSUE, "funding_without_progress"
    )
    # The allocation is not paused behind the person's card.
    assert not controls.task_snapshot(task["id"])["task_paused"]
    assert len(audits_of(db, "funding_review_requested")) == 1


def test_repair_issue_stall_never_spawns_another_repair(db, routine):
    task, policy, state = routine
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        row.body = "<!-- factory-problem:" + "a" * 64 + " -->\nrepair"
        session.add(row)
        session.commit()
    pause(task, policy, routine_pause())
    settle_funding_request(
        task, _pending(task), funding_decision(assessment=ASSESSMENT)
    )
    pause(task, policy, routine_pause(), 2)
    assert len(state["cards"]) == 1
    assert audits_of(db, funding.BLOCKER) == []


def test_concurrent_routine_pauses_request_one_review(db, routine):
    import threading

    task, policy, state = routine
    barrier = threading.Barrier(3)
    results = []

    def attempt():
        barrier.wait()
        try:
            results.append(funding.routine_pause(task, routine_pause()))
        except Exception as exc:  # noqa: BLE001 - a lost lock race may raise
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=attempt) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(audits_of(db, "funding_review_requested")) == 1
    # Exactly one pause owns the review; any other keeps its own card.
    assert results.count(True) == 1


def test_routine_pause_never_rides_another_pending_review(db, routine):
    task, policy, state = routine
    # A review already pending for a different trigger.
    assert funding.request(task, "Watchdog: assess the next tranche")
    pause(task, policy, routine_pause())
    assert len(state["cards"]) == 1
    assert len(audits_of(db, "funding_review_requested")) == 1


def test_exhausted_review_refusals_raise_the_routed_pause_card(
    db, routine, monkeypatch
):
    from datetime import timedelta

    task, policy, state = routine
    pause(task, policy, routine_pause())
    assert state["cards"] == []
    clock = {"now": controls._now()}
    monkeypatch.setattr(controls, "_now", lambda: clock["now"])
    for _ in range(funding.FUNDING_REFUSAL_LIMIT):
        request = _pending(task)
        assert request is not None
        # An unusable artifact settles as a refusal.
        settle_funding_request(task, request, {"action": "nonsense"})
        assert _settled(task)["refusal"]
        clock["now"] += timedelta(minutes=6)
        funding.reconcile(
            conductor._task(task["id"]),
            policy,
            conductor.graph.node_runs(task["id"]),
            controls.can_start(task["id"]),
        )
    assert state["cards"] == [routine_pause()["question"]]
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        assert row.state != "failed"


def test_scout_tasks_are_never_routed(db, routine, monkeypatch):
    from factory.orchestration import factory_improvement_scout as scout

    task, policy, state = routine
    monkeypatch.setattr(scout, "owns_task", lambda _task_id: True)
    pause(task, policy, routine_pause())
    assert len(state["cards"]) == 1
    assert audits_of(db, "funding_review_requested") == []


def test_a_refusal_run_after_a_judge_answer_does_not_revive_an_old_pause(
    db, routine, monkeypatch
):
    task, policy, state = routine
    pause(task, policy, routine_pause())
    # The judge answers without a grant (a stop); the routed pause is spent.
    settle_funding_request(
        task,
        _pending(task),
        funding_decision(
            action="stop",
            additional_work_turns=0,
            lease_minutes=0,
            assessment=ASSESSMENT,
        ),
    )
    assert _settled(task)["refusal"] is None
    assert funding._routed_pause(task["id"]) is None


def test_exhausted_refusals_with_an_unpostable_card_fail_instead_of_wedging(
    db, routine, monkeypatch
):
    from datetime import timedelta

    task, policy, state = routine
    pause(task, policy, routine_pause())

    def refuse(*_args, **_kwargs):
        raise conductor._EditRefused("pause_without_question", "no question")

    monkeypatch.setattr(conductor, "_escalate_task", refuse)
    clock = {"now": controls._now()}
    monkeypatch.setattr(controls, "_now", lambda: clock["now"])
    for _ in range(funding.FUNDING_REFUSAL_LIMIT):
        settle_funding_request(task, _pending(task), {"action": "nonsense"})
        clock["now"] += timedelta(minutes=6)
        funding.reconcile(
            conductor._task(task["id"]),
            policy,
            conductor.graph.node_runs(task["id"]),
            controls.can_start(task["id"]),
        )
    with Session(db) as session:
        assert session.exec(select(FactoryReceipt)).one().state == "failed"


def test_github_errors_while_raising_the_routed_card_retry_not_fail(
    db, routine, monkeypatch
):
    from datetime import timedelta

    task, policy, state = routine
    pause(task, policy, routine_pause())

    def unreadable(*_args, **_kwargs):
        raise ValueError("GitHub response exceeds factory limit")

    monkeypatch.setattr(conductor, "_escalate_task", unreadable)
    clock = {"now": controls._now()}
    monkeypatch.setattr(controls, "_now", lambda: clock["now"])
    for attempt in range(funding.FUNDING_REFUSAL_LIMIT):
        settle_funding_request(task, _pending(task), {"action": "nonsense"})
        clock["now"] += timedelta(minutes=6)
        args = (
            conductor._task(task["id"]),
            policy,
            conductor.graph.node_runs(task["id"]),
            controls.can_start(task["id"]),
        )
        if attempt < funding.FUNDING_REFUSAL_LIMIT - 1:
            funding.reconcile(*args)
        else:
            with pytest.raises(ValueError):
                funding.reconcile(*args)
    with Session(db) as session:
        assert session.exec(select(FactoryReceipt)).one().state != "failed"


def _set_hold(db, task, hold):
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        direction = json.loads(row.direction_json or "{}")
        if hold == "landing_hold":
            direction["landing_hold"] = {"actor": "operator"}
        elif hold == "nonrenewable_recovery":
            direction["nonrenewable_recovery"] = {"actor": "operator"}
        row.direction_json = json.dumps(direction)
        session.add(row)
        session.commit()
    if hold in ("human_handoff", "review_recovery_authorized"):
        with controls._locked_session() as (session, _control):
            controls._audit(session, "operator", hold, task_id=task["id"])


HOLDS = (
    "landing_hold",
    "nonrenewable_recovery",
    "human_handoff",
    "review_recovery_authorized",
)


@pytest.mark.parametrize("hold", HOLDS)
def test_hold_set_while_labels_are_read_still_wins_under_the_lock(
    db, routine, monkeypatch, hold
):
    task, policy, state = routine
    original = conductor.github_get

    def racing_read(repo, suffix):
        # The hold lands after the unlocked pre-check, mid label read.
        _set_hold(db, task, hold)
        return original(repo, suffix)

    monkeypatch.setattr(conductor, "github_get", racing_read)
    pause(task, policy, routine_pause())
    assert audits_of(db, "funding_review_requested") == []
    assert len(state["cards"]) == 1
    # The routine path never mutates the held receipt itself.
    assert not controls.task_snapshot(task["id"])["task_paused"]


@pytest.mark.parametrize("hold", (*HOLDS, "label"))
def test_hold_or_label_set_before_settlement_blocks_the_grant(
    db, routine, monkeypatch, hold
):
    task, policy, state = routine
    pause(task, policy, routine_pause())
    request = _pending(task)
    assert state["cards"] == []
    if hold == "label":
        state["labels"] = ["human"]
    else:
        _set_hold(db, task, hold)
    settle_funding_request(task, request, funding_decision(assessment=ASSESSMENT))
    assert _grant(task) is None
    if hold == "nonrenewable_recovery":
        # Its existing locked owner pauses the task for the operator first.
        assert controls.task_snapshot(task["id"])["task_paused"]
        return
    assert _settled(task)["refusal"].startswith("routine pause held")
    assert state["cards"] == [routine_pause()["question"]]


def test_held_refusal_retries_the_card_without_another_review(db, routine, monkeypatch):
    task, policy, state = routine
    pause(task, policy, routine_pause())
    request = _pending(task)
    _set_hold(db, task, "landing_hold")
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise ValueError("GitHub response exceeds factory limit")
        state["cards"].append(args[1]["question"])

    monkeypatch.setattr(conductor, "_escalate_task", flaky)
    with pytest.raises(ValueError):
        settle_funding_request(task, request, funding_decision(assessment=ASSESSMENT))
    assert _settled(task)["failure_category"] == "routine_pause_held"
    from datetime import timedelta

    later = controls._now() + timedelta(minutes=6)
    monkeypatch.setattr(controls, "_now", lambda: later)
    funding.reconcile(
        conductor._task(task["id"]),
        policy,
        conductor.graph.node_runs(task["id"]),
        controls.can_start(task["id"]),
    )
    assert state["cards"] == [routine_pause()["question"]]
    assert len(audits_of(db, "funding_review_requested")) == 1


def test_routine_provenance_is_persisted_on_the_review_request(db, routine):
    task, policy, state = routine
    pause(task, policy, routine_pause())
    request = _pending(task)
    assert request["routine_pause"]["cause"] == "factory-decision:conductor_1:1"
    assert request["routine_pause"]["decision"] == routine_pause()


@pytest.mark.parametrize("hold", HOLDS)
def test_hold_inside_request_before_its_lock_still_wins(db, routine, monkeypatch, hold):
    """The window inside request(): after its unlocked reads, before the lock."""
    task, policy, state = routine
    original = funding._issue

    def racing_issue(task_arg):
        _set_hold(db, task, hold)
        return original(task_arg)

    monkeypatch.setattr(funding, "_issue", racing_issue)
    pause(task, policy, routine_pause())
    assert audits_of(db, "funding_review_requested") == []
    assert len(state["cards"]) == 1
    assert not controls.task_snapshot(task["id"])["task_paused"]


@pytest.mark.parametrize("control", ["pause_admissions", "stop"])
def test_lane_paused_between_request_and_settle_never_grants(db, routine, control):
    task, policy, state = routine
    pause(task, policy, routine_pause())
    request = _pending(task)
    assert controls.set_control(control, "operator")["ok"]
    settle_funding_request(task, request, funding_decision(assessment=ASSESSMENT))
    assert _grant(task) is None
    assert _settled(task) is None
    assert state["cards"] == []
    if control == "pause_admissions":
        assert controls.set_control("enable", "operator")["ok"]
        settle_funding_request(task, request, funding_decision(assessment=ASSESSMENT))
        assert _grant(task) is not None


def test_retry_of_a_routed_review_keeps_provenance_and_its_locked_checks(
    db, routine, monkeypatch
):
    from datetime import timedelta

    task, policy, state = routine
    pause(task, policy, routine_pause())
    settle_funding_request(task, _pending(task), {"action": "nonsense"})
    clock = {"now": controls._now() + timedelta(minutes=6)}
    monkeypatch.setattr(controls, "_now", lambda: clock["now"])
    # A hold lands before the retry: the retry is refused under the lock.
    _set_hold(db, task, "landing_hold")
    args = (
        conductor._task(task["id"]),
        policy,
        conductor.graph.node_runs(task["id"]),
        controls.can_start(task["id"]),
    )
    assert funding.reconcile(*args) is True
    # No second paid review, and the held question reaches a person.
    assert len(audits_of(db, "funding_review_requested")) == 1
    assert state["cards"] == [routine_pause()["question"]]


def test_transient_refusal_of_a_routed_retry_only_retries(db, routine, monkeypatch):
    from datetime import timedelta

    task, policy, state = routine
    pause(task, policy, routine_pause())
    settle_funding_request(task, _pending(task), {"action": "nonsense"})
    clock = {"now": controls._now() + timedelta(minutes=6)}
    monkeypatch.setattr(controls, "_now", lambda: clock["now"])
    monkeypatch.setattr(
        controls, "execution_capacity_available", lambda *_args, **_kwargs: False
    )
    funding.reconcile(
        conductor._task(task["id"]),
        policy,
        conductor.graph.node_runs(task["id"]),
        controls.can_start(task["id"]),
    )
    assert state["cards"] == []
    assert len(audits_of(db, "funding_review_requested")) == 1


def test_settle_uses_the_exact_request_provenance(db, routine, monkeypatch):
    task, policy, state = routine
    pause(task, policy, routine_pause())
    request = _pending(task)
    # Even if the run lookup misses it, the request's own provenance holds.
    monkeypatch.setattr(funding, "_routed_pause", lambda _task_id: None)
    _set_hold(db, task, "landing_hold")
    settle_funding_request(task, request, funding_decision(assessment=ASSESSMENT))
    assert _grant(task) is None
    assert _settled(task)["failure_category"] == "routine_pause_held"


def _mirror_label(db, label):
    """Set a label on the receipt's synchronized work-item mirror."""
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        if row.work_item_id is None:
            item = WorkItem(
                title="mirror",
                state="ready",
                source_kind="github",
                trust="trusted",
                authority="github",
                github_repo=REPO,
                github_issue_number=ISSUE,
                labels=[],
            )
            session.add(item)
            session.flush()
            row.work_item_id = item.id
            session.add(row)
        item = session.get(WorkItem, row.work_item_id)
        if label is not None:
            item.labels = [*item.labels, label]
        session.add(item)
        session.commit()


@pytest.mark.parametrize(
    "label", ["human", "needs-human", "needs-thought", "security-finding", "security"]
)
def test_mirrored_label_after_the_github_read_is_revalidated_before_the_grant(
    db, routine, label
):
    task, policy, state = routine
    pause(task, policy, routine_pause())
    request = _pending(task)
    # GitHub's settlement-time read still sees no label; the synchronized
    # mirror records it before the locked grant decision.
    _mirror_label(db, label)
    settle_funding_request(task, request, funding_decision(assessment=ASSESSMENT))
    assert _grant(task) is None
    assert _settled(task)["refusal"] == "routine pause held: held_label"
    assert state["cards"] == [routine_pause()["question"]]


def test_mirrored_label_between_preflight_and_request_lock_wins(
    db, routine, monkeypatch
):
    task, policy, state = routine
    original = funding._issue

    def racing_issue(task_arg):
        _mirror_label(db, "human")
        return original(task_arg)

    monkeypatch.setattr(funding, "_issue", racing_issue)
    pause(task, policy, routine_pause())
    assert audits_of(db, "funding_review_requested") == []
    assert len(state["cards"]) == 1


def test_unrelated_mirror_labels_do_not_hold(db, routine):
    task, policy, state = routine
    _mirror_label(db, "bug")
    pause(task, policy, routine_pause())
    settle_funding_request(
        task, _pending(task), funding_decision(assessment=ASSESSMENT)
    )
    assert _grant(task) is not None


def test_receipt_without_a_label_mirror_is_never_routine(db, routine):
    task, policy, state = routine
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        row.work_item_id = None
        session.add(row)
        session.commit()
    pause(task, policy, routine_pause())
    assert audits_of(db, "funding_review_requested") == []
    assert len(state["cards"]) == 1


def test_retry_refused_by_a_mirrored_label_raises_the_card(db, routine, monkeypatch):
    from datetime import timedelta

    task, policy, state = routine
    pause(task, policy, routine_pause())
    settle_funding_request(task, _pending(task), {"action": "nonsense"})
    later = controls._now() + timedelta(minutes=6)
    monkeypatch.setattr(controls, "_now", lambda: later)
    _mirror_label(db, "needs-human")
    assert funding.reconcile(
        conductor._task(task["id"]),
        policy,
        conductor.graph.node_runs(task["id"]),
        controls.can_start(task["id"]),
    )
    assert state["cards"] == [routine_pause()["question"]]
    assert len(audits_of(db, "funding_review_requested")) == 1


def test_held_retry_with_an_unpostable_card_ends_instead_of_wedging(
    db, routine, monkeypatch
):
    from datetime import timedelta

    task, policy, state = routine
    pause(task, policy, routine_pause())
    settle_funding_request(task, _pending(task), {"action": "nonsense"})
    later = controls._now() + timedelta(minutes=6)
    monkeypatch.setattr(controls, "_now", lambda: later)
    _set_hold(db, task, "landing_hold")

    def refuse(*_args, **_kwargs):
        raise conductor._EditRefused("pause_without_question", "no question")

    monkeypatch.setattr(conductor, "_escalate_task", refuse)
    assert funding.reconcile(
        conductor._task(task["id"]),
        policy,
        conductor.graph.node_runs(task["id"]),
        controls.can_start(task["id"]),
    )
    with Session(db) as session:
        assert session.exec(select(FactoryReceipt)).one().state == "failed"
    assert len(audits_of(db, "funding_review_requested")) == 1
