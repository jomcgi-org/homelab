"""Factory pull request settlement, retirement, and readmission lifecycle."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import factory.orchestration.factory_controls as controls
import factory.orchestration.factory_pr_lifecycle as lifecycle
from factory.orchestration import factory_conductor as conductor
from factory.orchestration import factory_gates as gates
from factory.orchestration.factory_intake import receive_issue
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask

REPO = "owner/repo"
NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
FIXTURES = Path(__file__).with_name("fixtures") / "pr_lifecycle_20260919.json"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pr-lifecycle.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                SwarmTask,
                SwarmNodeRun,
                FactoryControl,
                FactoryReceipt,
                FactoryAudit,
                WorkItem,
            )
        ],
    )
    with Session(engine) as session:
        session.add(
            FactoryControl(
                id="factory",
                actor="test",
                state="enabled",
                policy_json=json.dumps({"repo": REPO}),
            )
        )
        session.commit()
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(controls, "_now", lambda: NOW)
    yield engine
    engine.dispose()


def pull(number, issue, branch=None, *, state="open", draft=False):
    return {
        "number": number,
        "state": state,
        "draft": draft,
        "body": f"Closes #{issue}",
        "node_id": f"PR_{number}",
        "head": {
            "ref": branch or f"factory/t-{number}",
            "repo": {"full_name": REPO},
        },
    }


class Github:
    def __init__(self, pulls=(), issues=None):
        self.pulls = {item["number"]: dict(item) for item in pulls}
        self.issues = dict(issues or {})
        self.comments = {}
        self.writes = []
        self.lists = []
        self.fail_close = 0
        self.fail_pull = set()

    def get(self, repo, path):
        assert repo == REPO
        kind, raw = path.split("/", 1)
        number = int(raw)
        if kind == "pulls":
            if number in self.fail_pull:
                raise RuntimeError("pull read unavailable")
            return dict(self.pulls[number])
        return dict(self.issues[number])

    def list(self, repo, path):
        assert repo == REPO
        self.lists.append(path)
        parsed = urlparse("https://example.invalid/" + path)
        parsed_path = parsed.path.lstrip("/")
        query = parse_qs(parsed.query)
        if parsed_path == "pulls":
            rows = [row for row in self.pulls.values() if row["state"] == "open"]
            head = query.get("head", [None])[0]
            if head:
                branch = head.split(":", 1)[1]
                rows = [row for row in rows if row["head"]["ref"] == branch]
            rows.sort(key=lambda row: row["number"])
            size = int(query.get("per_page", [20])[0])
            page = int(query.get("page", [1])[0])
            return [dict(row) for row in rows[(page - 1) * size : page * size]]
        if parsed_path.startswith("issues/") and parsed_path.endswith("/comments"):
            number = int(parsed_path.split("/")[1])
            rows = self.comments.get(number, [])
            size = int(query.get("per_page", [100])[0])
            page = int(query.get("page", [1])[0])
            return [dict(row) for row in rows[(page - 1) * size : page * size]]
        raise AssertionError(path)

    def write(self, repo, path, payload, *, method="POST"):
        assert repo == REPO
        self.writes.append((method, path, dict(payload)))
        if path.endswith("/comments"):
            number = int(path.split("/")[1])
            self.comments.setdefault(number, []).append({"body": payload["body"]})
            return {"id": len(self.comments[number])}
        number = int(path.split("/")[1])
        if self.fail_close:
            self.fail_close -= 1
            raise RuntimeError("close unavailable")
        assert method == "PATCH" and payload == {"state": "closed"}
        self.pulls[number]["state"] = "closed"
        return dict(self.pulls[number])

    def graphql(self, _query, variables):
        node = variables["pullRequestId"]
        target = next(row for row in self.pulls.values() if row["node_id"] == node)
        target["draft"] = True
        return {
            "convertPullRequestToDraft": {"pullRequest": {"number": target["number"]}}
        }


@pytest.fixture
def github(monkeypatch):
    def install(value):
        monkeypatch.setattr(lifecycle, "github_get", value.get)
        monkeypatch.setattr(lifecycle, "github_list", value.list)
        monkeypatch.setattr(lifecycle, "github_write", value.write)
        monkeypatch.setattr(lifecycle, "github_graphql", value.graphql)
        return value

    return install


def task_receipt(
    db,
    task_id,
    issue,
    state,
    *,
    branch=None,
    pr_number=None,
    reason=None,
    generation=0,
):
    direction = None
    if branch and pr_number:
        direction = json.dumps(
            {
                "delivery_branch": branch,
                "delivery_pr_number": pr_number,
                "delivery_adoption": True,
                "delivery_target_checked": True,
            }
        )
    with Session(db) as session:
        session.add(
            SwarmTask(
                id=task_id,
                task_text="test",
                repo=REPO,
                base_branch="main",
                conductor_model="opus",
                start_state=state,
            )
        )
        session.flush()
        receipt = FactoryReceipt(
            repo=REPO,
            issue_number=issue,
            generation=generation,
            title="test",
            body="",
            url=f"https://github.com/{REPO}/issues/{issue}",
            actor="test",
            state=state,
            task_id=task_id,
            direction_json=direction,
        )
        session.add(receipt)
        session.flush()
        if state not in ("admitted", "uncertain"):
            session.add(
                FactoryAudit(
                    actor="test",
                    action="finish_task",
                    task_id=task_id,
                    detail_json=json.dumps(
                        {
                            "outcome": state,
                            "evidence": ({"reason": reason} if reason else None),
                        }
                    ),
                )
            )
        session.commit()
        return receipt.id


def audit_details(db, action):
    with Session(db) as session:
        return [
            json.loads(row.detail_json)
            for row in session.exec(
                select(FactoryAudit)
                .where(FactoryAudit.action == action)
                .order_by(FactoryAudit.id)
            ).all()
        ]


def duplicate_pairs():
    return json.loads(FIXTURES.read_text())


@pytest.mark.parametrize(
    "case",
    duplicate_pairs(),
    ids=lambda case: "-".join(str(pr["number"]) for pr in case["pulls"]),
)
def test_historical_duplicate_pairs_replay_recorded_outcomes(db, github, case):
    pulls = [
        pull(item["number"], case["issue"], item["branch"]) for item in case["pulls"]
    ]
    merged = [item for item in case["pulls"] if item["outcome"] == "merged"]
    closed = [item for item in case["pulls"] if item["outcome"] == "closed_unmerged"]

    if merged:
        assert len(merged) == len(closed) == 1
        survivor = merged[0]
        stale = closed[0]
        task_receipt(
            db,
            f"survivor-{survivor['number']}",
            case["issue"],
            "admitted",
            branch=survivor["branch"],
            pr_number=survivor["number"],
        )
        api = github(Github(pulls, {case["issue"]: {"state": "open"}}))

        lifecycle._retire_pull(REPO, api.pulls[stale["number"]])

        assert api.pulls[stale["number"]]["state"] == "closed"
        assert api.pulls[survivor["number"]]["state"] == "open"
        assert f"PR #{survivor['number']}" in api.comments[stale["number"]][0]["body"]
        detail = audit_details(db, "factory_pr_retired")[-1]
        assert detail["pr_number"] == stale["number"]
        assert detail["survivor_pr_number"] == survivor["number"]
    else:
        assert len(closed) == len(pulls)
        api = github(Github(pulls, {case["issue"]: {"state": "closed"}}))

        lifecycle.sweep_stale_prs(REPO)

        assert all(api.pulls[item["number"]]["state"] == "closed" for item in closed)
        retired = audit_details(db, "factory_pr_retired")
        assert {item["number"] for item in closed} == {
            detail["pr_number"] for detail in retired
        }


def test_running_owner_is_named_and_survivor_is_never_closed(db, github):
    old = pull(10, 7, "factory/old")
    new = pull(20, 7, "factory/new")
    task_receipt(db, "new", 7, "admitted", branch="factory/new", pr_number=20)
    api = github(Github([old, new], {7: {"state": "open"}}))

    lifecycle._retire_pull(REPO, old)

    assert api.pulls[10]["state"] == "closed"
    assert api.pulls[20]["state"] == "open"
    assert "PR #20" in api.comments[10][0]["body"]
    assert "running task `new`" in api.comments[10][0]["body"]
    assert audit_details(db, "factory_pr_retired")[-1]["survivor_pr_number"] == 20


def test_survivor_retirement_names_the_shared_closing_issue(db, github):
    old = pull(21, 7, "factory/old")
    new = pull(22, 99, "factory/new")
    new["body"] += "\nCloses #7"
    task_receipt(db, "new", 7, "admitted", branch="factory/new", pr_number=22)
    api = github(Github([old, new], {7: {"state": "open"}}))

    lifecycle._retire_pull(REPO, old)

    assert "issue #7" in api.comments[21][0]["body"]
    assert "issue #99" not in api.comments[21][0]["body"]
    assert audit_details(db, "factory_pr_retired")[-1]["issue_number"] == 7


def test_active_owner_and_non_factory_branch_are_never_retired(db, github):
    active = pull(30, 8, "factory/active")
    human = pull(31, 9, "fix/human")
    task_receipt(db, "active", 8, "admitted", branch="factory/active", pr_number=30)
    api = github(
        Github([active, human], {8: {"state": "closed"}, 9: {"state": "closed"}})
    )

    lifecycle.sweep_stale_prs(REPO)

    assert api.pulls[30]["state"] == "open"
    assert api.pulls[31]["state"] == "open"
    assert not api.comments
    assert not audit_details(db, "factory_pr_retired")


def test_partial_close_failure_retries_without_duplicate_comment(db, github):
    stale = pull(40, 10)
    api = github(Github([stale], {10: {"state": "closed"}}))
    api.fail_close = 1

    with pytest.raises(RuntimeError, match="close unavailable"):
        lifecycle._retire_pull(REPO, stale)
    lifecycle._retire_pull(REPO, stale)

    assert api.pulls[40]["state"] == "closed"
    assert len(api.comments[40]) == 1
    assert len(audit_details(db, "factory_pr_retired")) == 1


def test_retirement_intent_ignores_unrelated_and_malformed_audits(db):
    with Session(db) as session:
        session.add_all(
            [
                FactoryAudit(
                    actor="test",
                    action="factory_pr_retired",
                    detail_json="not json",
                ),
                FactoryAudit(
                    actor="test",
                    action="factory_pr_retired",
                    detail_json=(f'{{"pr_number":41,"repo":"{REPO}","unterminated":'),
                ),
            ]
        )
        session.commit()

    lifecycle._record_retirement_intent(REPO, 41, issue_number=10)
    lifecycle._record_retirement_intent(REPO, 41, issue_number=10)

    with Session(db) as session:
        rows = session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "factory_pr_retired",
                FactoryAudit.actor == lifecycle.ACTOR,
            )
        ).all()
        assert len(rows) == 1
        assert json.loads(rows[0].detail_json)["pr_number"] == 41


def test_successor_read_failure_leaves_candidate_open(db, github):
    old = pull(50, 11, "factory/old")
    new = pull(51, 11, "factory/new")
    task_receipt(db, "new", 11, "admitted", branch="factory/new", pr_number=51)
    api = github(Github([old, new], {11: {"state": "open"}}))
    api.fail_pull.add(51)

    with pytest.raises(RuntimeError, match="unavailable"):
        lifecycle._retire_pull(REPO, old)

    assert api.pulls[50]["state"] == "open"
    assert not api.comments


def test_sweep_cursor_reaches_later_candidates(db, github):
    pulls = [pull(number, 100 + number) for number in range(1, 22)]
    issues = {100 + number: {"state": "open"} for number in range(1, 21)}
    issues[121] = {"state": "closed"}
    api = github(Github(pulls, issues))

    lifecycle.sweep_stale_prs(REPO)
    lifecycle.sweep_stale_prs(REPO)

    assert "page=1" in api.lists[0]
    assert any(path.startswith("pulls?") and "page=2" in path for path in api.lists)
    assert api.pulls[21]["state"] == "closed"


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("escalated", "operator decision required"),
        ("cancelled", "operator cancelled the task"),
        ("failed", "implementation failed"),
        ("escalated", "Absolute task deadline elapsed"),
    ],
    ids=["escalated", "cancelled", "failed", "deadline-expired"],
)
def test_non_success_settlement_drafts_once_and_names_re_admission(
    db, github, state, reason
):
    branch = f"factory/{state}-{reason[:4]}"
    number = 70 + len(reason)
    receipt_id = task_receipt(
        db,
        state + reason[:3],
        12,
        state,
        branch=branch,
        pr_number=number,
        reason=reason,
    )
    api = github(Github([pull(number, 12, branch)], {12: {"state": "open"}}))

    lifecycle.draft_settled_prs(REPO)
    lifecycle.draft_settled_prs(REPO)

    assert api.pulls[number]["draft"] is True
    assert len(api.comments[number]) == 1
    body = api.comments[number][0]["body"]
    assert f"receipt `{receipt_id}`" in body
    assert reason in body
    assert f"adopts branch `{branch}`" in body


def test_succeeded_settlement_leaves_pull_alone(db, github):
    branch = "factory/succeeded"
    task_receipt(db, "succeeded", 13, "succeeded", branch=branch, pr_number=80)
    api = github(Github([pull(80, 13, branch)], {13: {"state": "open"}}))

    lifecycle.draft_settled_prs(REPO)

    assert api.pulls[80]["draft"] is False
    assert not api.comments


def test_settlement_uses_latest_run_artifact_when_receipt_has_no_target(db, github):
    task_receipt(db, "artifact", 17, "failed", reason="review failed")
    with Session(db) as session:
        session.add(
            SwarmNodeRun(
                task_id="artifact",
                node_key="implement_fix",
                attempt=1,
                status="failed",
                outcome_json=json.dumps({"artifact": {"pr_number": 110}}),
            )
        )
        session.commit()
    api = github(Github([pull(110, 17, "factory/artifact")], {17: {"state": "open"}}))

    lifecycle.draft_settled_prs(REPO)

    assert api.pulls[110]["draft"] is True
    assert "review failed" in api.comments[110][0]["body"]


def test_successor_adoption_prevents_settlement_from_drafting(db, github):
    branch = "factory/shared"
    task_receipt(db, "old", 14, "failed", branch=branch, pr_number=90)
    task_receipt(
        db,
        "new",
        14,
        "admitted",
        branch=branch,
        pr_number=90,
        generation=1,
    )
    api = github(Github([pull(90, 14, branch)], {14: {"state": "open"}}))

    lifecycle.draft_settled_prs(REPO)

    assert api.pulls[90]["draft"] is False
    assert not api.comments
    assert (
        audit_details(db, "factory_pr_settlement_complete")[-1]["successor_task_id"]
        == "new"
    )


def test_operator_receipt_is_created_with_safe_delivery_target(db, monkeypatch):
    existing = pull(100, 15, "factory/existing")
    monkeypatch.setattr(conductor, "github_list", lambda *_args: [existing])

    target = gates.receive_delivery_target(REPO, 15)
    received = receive_issue(
        REPO,
        15,
        "test",
        "body",
        f"https://github.com/{REPO}/issues/15",
        "operator",
        delivery_target=target,
    )

    with Session(db) as session:
        row = session.get(FactoryReceipt, received["receipt"]["id"])
        direction = json.loads(row.direction_json)
        assert direction["delivery_pr_number"] == 100
        assert direction["delivery_branch"] == "factory/existing"
        assert direction["delivery_adoption"] is True
        assert "delivery_target_checked" not in direction


@pytest.mark.parametrize("adopted", [False, True])
def test_staged_delivery_is_found_from_receipt_without_closing_issue(
    db, monkeypatch, adopted
):
    branch = "factory/earlier-adoption" if adopted else "factory/earlier"
    task_receipt(
        db,
        "earlier",
        15,
        "cancelled",
        branch=branch if adopted else None,
        pr_number=100 if adopted else None,
    )
    existing = pull(100, 15, branch)
    existing["body"] = "Repository-only staged delivery. Issue #15 remains open."
    unrelated = pull(101, 15, "factory/unrelated")
    unrelated["body"] = existing["body"]
    fork = {
        **existing,
        "number": 102,
        "head": {
            "ref": branch,
            "repo": {"full_name": "someone/fork"},
        },
    }
    monkeypatch.setattr(
        conductor, "github_list", lambda *_: [unrelated, fork, existing]
    )

    assert gates.receive_delivery_target(REPO, 15) == {
        "delivery_branch": branch,
        "delivery_pr_number": 100,
        "delivery_adoption": True,
    }
    assert gates.receive_delivery_target(REPO, 16) is None
    if adopted:
        monkeypatch.setattr(
            conductor, "github_list", lambda *_: [{**existing, "number": 103}]
        )
        assert gates.receive_delivery_target(REPO, 15) is None


def test_staged_delivery_cannot_take_an_active_recorded_branch(db, monkeypatch):
    task_receipt(db, "earlier", 15, "cancelled")
    task_receipt(db, "owner", 99, "admitted", branch="factory/earlier", pr_number=100)
    existing = pull(100, 15, "factory/earlier")
    existing["body"] = "Staged delivery; operational acceptance remains."
    monkeypatch.setattr(conductor, "github_list", lambda *_: [existing])

    assert gates.receive_delivery_target(REPO, 15) is None


def test_receipt_target_is_reverified_before_adoption(db, monkeypatch):
    initial = pull(102, 18, "factory/initial")
    monkeypatch.setattr(conductor, "github_list", lambda *_args: [initial])
    target = gates.receive_delivery_target(REPO, 18)
    assert target is not None
    received = receive_issue(
        REPO,
        18,
        "test",
        "body",
        f"https://github.com/{REPO}/issues/18",
        "operator",
        delivery_target=target,
    )
    with Session(db) as session:
        session.add(
            SwarmTask(
                id="replacement",
                task_text="test",
                repo=REPO,
                base_branch="main",
                conductor_model="opus",
                start_state="admitted",
            )
        )
        session.flush()
        row = session.get(FactoryReceipt, received["receipt"]["id"])
        row.task_id = "replacement"
        row.state = "admitted"
        session.add(row)
        session.commit()

    replacement = pull(103, 18, "factory/replacement")
    monkeypatch.setattr(conductor, "github_list", lambda *_args: [replacement])
    task = {
        "id": "replacement",
        "repo": REPO,
        "issue_number": 18,
        "base_branch": "main",
        **target,
        "delivery_target_checked": False,
    }

    assert gates.adopt_delivery(task)
    assert task["delivery_pr_number"] == 103
    assert task["delivery_branch"] == "factory/replacement"
    assert task["delivery_target_checked"] is True
    with Session(db) as session:
        row = session.get(FactoryReceipt, received["receipt"]["id"])
        direction = json.loads(row.direction_json)
        assert direction["delivery_pr_number"] == 103
        assert direction["delivery_target_checked"] is True


@pytest.mark.parametrize("branch", ["fix/human", "factory/owned"])
def test_receive_target_never_adopts_unauthorized_or_running_branch(
    db, monkeypatch, branch
):
    existing = pull(101, 16, branch)
    if branch == "factory/owned":
        task_receipt(db, "owner", 99, "admitted", branch=branch, pr_number=101)
    monkeypatch.setattr(conductor, "github_list", lambda *_args: [existing])

    assert gates.receive_delivery_target(REPO, 16) is None
