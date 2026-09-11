"""Landing an approved delivery: arming one merge, then closing its issue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import swarm.factory_controls as controls
import swarm.factory_landing as landing
from swarm.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from swarm.models import SwarmTask

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
POLICY = {"repo": "owner/repo", "auto_merge": True}


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'landing.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                SwarmTask,
                FactoryControl,
                FactoryReceipt,
                FactoryStart,
                FactoryAudit,
            )
        ],
    )
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(landing, "_now", lambda: NOW)
    yield engine
    engine.dispose()


def delivered(db, task_id, issue_number, pr_number, *, evidence=True):
    """One settled delivery: its task, its succeeded receipt and its evidence."""
    with Session(db) as session:
        session.add(
            SwarmTask(
                id=task_id,
                task_text="issue",
                repo="owner/repo",
                base_branch="main",
                conductor_model="opus",
                budget_usd=5.0,
                workflow_id=f"factory:{task_id}",
                start_state="factory",
                start_triggered_by="test",
            )
        )
        session.flush()
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=issue_number,
                generation=0,
                title="issue",
                body="",
                url=f"https://github.com/owner/repo/issues/{issue_number}",
                actor="test",
                state="succeeded",
                task_id=task_id,
                updated_at=NOW - timedelta(minutes=5),
            )
        )
        session.flush()
        session.add(
            FactoryAudit(
                actor="factory:reconciler",
                action="finish_task",
                task_id=task_id,
                detail_json=json.dumps(
                    {
                        "outcome": "succeeded",
                        "evidence": (
                            {
                                "pr_url": (
                                    f"https://github.com/owner/repo/pull/{pr_number}"
                                ),
                                "head_sha": "a" * 40,
                                "state": "ready_for_review",
                            }
                            if evidence
                            else {"state": "advisory_comment"}
                        ),
                    }
                ),
            )
        )
        session.commit()


def audits(db, action, task_id=None):
    with Session(db) as session:
        query = select(FactoryAudit).where(FactoryAudit.action == action)
        if task_id is not None:
            query = query.where(FactoryAudit.task_id == task_id)
        return [
            json.loads(row.detail_json)
            for row in session.exec(query.order_by(FactoryAudit.id)).all()
        ]


def github(monkeypatch, *, pulls=None, issues=None):
    """Fake the read and write boundary, and count every call it takes."""
    calls = {"get": [], "graphql": [], "write": []}

    def get(_repo, suffix):
        calls["get"].append(suffix)
        if suffix.startswith("pulls/"):
            return dict((pulls or {})[int(suffix.split("/")[1])])
        return dict((issues or {})[int(suffix.split("/")[1])])

    def graphql(_query, variables):
        calls["graphql"].append(variables)
        return {"enablePullRequestAutoMerge": {"pullRequest": {"number": 1}}}

    def write(_repo, suffix, payload, *, method="POST"):
        calls["write"].append((method, suffix, payload))
        return {}

    monkeypatch.setattr(landing, "github_get", get)
    monkeypatch.setattr(landing, "github_graphql", graphql)
    monkeypatch.setattr(landing, "github_write", write)
    return calls


def pull(number, *, merged=False, state="open", draft=False):
    return {
        "number": number,
        "state": "closed" if merged else state,
        "draft": draft,
        "merged": merged,
        "node_id": f"PR_{number}",
        "merge_commit_sha": "b" * 40 if merged else None,
    }


def test_landing_is_inert_without_the_flag(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    monkeypatch.setattr(
        landing,
        "github_get",
        lambda *_args: pytest.fail("landing read GitHub with auto_merge off"),
    )
    landing.landing_tick({"repo": "owner/repo"})
    landing.landing_tick({"repo": "owner/repo", "auto_merge": False})
    assert audits(db, "merge_armed") == []


def test_landing_arms_one_merge_and_does_not_arm_it_twice(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    calls = github(monkeypatch, pulls={3: pull(3)})
    landing.landing_tick(POLICY)
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]
    assert audits(db, "merge_armed", "t-1") == [
        {"pr_number": 3, "head_sha": "a" * 40, "merge_method": "rebase"}
    ]
    # A second tick observes the armed pull request; it never arms it again.
    landing.landing_tick(POLICY)
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]
    assert len(audits(db, "merge_armed")) == 1


def test_landing_defers_a_second_delivery_while_one_is_armed(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    delivered(db, "t-2", 12, 4)
    calls = github(monkeypatch, pulls={3: pull(3), 4: pull(4)})
    landing.landing_tick(POLICY)
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]
    landing.landing_tick(POLICY)
    # The merge queue ejects everything behind a failed candidate, so exactly
    # one factory pull request is ever armed.
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]
    deferred = audits(db, "merge_deferred", "t-2")
    assert deferred == [
        {"pr_number": 4, "blocked_by_pr": 3, "blocked_by_task_id": "t-1"}
    ]
    # The deferral is recorded once per blocker, not once per tick.
    landing.landing_tick(POLICY)
    assert len(audits(db, "merge_deferred", "t-2")) == 1


def test_a_merge_records_the_merge_and_closes_the_issue(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    pulls = {3: pull(3)}
    issues = {11: {"number": 11, "state": "open"}}
    calls = github(monkeypatch, pulls=pulls, issues=issues)
    landing.landing_tick(POLICY)
    pulls[3] = pull(3, merged=True)
    landing.landing_tick(POLICY)
    merged = audits(db, "merged", "t-1")
    assert merged == [
        {
            "pr_number": 3,
            "merge_commit_sha": "b" * 40,
            "armed_by_factory": True,
            # Phase 4 stops at the merge: nothing has checked the rollout.
            "rollout_verified": None,
        }
    ]
    assert calls["write"] == [
        ("POST", "issues/11/comments", calls["write"][0][2]),
        ("PATCH", "issues/11", {"state": "closed", "state_reason": "completed"}),
    ]
    assert "#3" in calls["write"][0][2]["body"]
    assert audits(db, "issue_closed", "t-1") == [
        {"issue_number": 11, "pr_number": 3, "closed_by_factory": True}
    ]
    # Landing is finished for this task and touches GitHub no further.
    before = len(calls["get"])
    landing.landing_tick(POLICY)
    assert len(calls["get"]) == before


def test_an_issue_already_closed_is_recorded_without_being_rewritten(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    calls = github(
        monkeypatch,
        pulls={3: pull(3, merged=True)},
        issues={11: {"number": 11, "state": "closed"}},
    )
    landing.landing_tick(POLICY)
    landing.landing_tick(POLICY)
    assert calls["write"] == []
    assert audits(db, "issue_closed", "t-1") == [
        {"issue_number": 11, "pr_number": 3, "closed_by_factory": False}
    ]


def test_a_merge_frees_the_arming_slot_in_the_same_tick(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    delivered(db, "t-2", 12, 4)
    pulls = {3: pull(3), 4: pull(4)}
    calls = github(
        monkeypatch, pulls=pulls, issues={11: {"number": 11, "state": "closed"}}
    )
    landing.landing_tick(POLICY)
    pulls[3] = pull(3, merged=True)
    landing.landing_tick(POLICY)
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}, {"pullRequestId": "PR_4"}]
    assert audits(db, "merge_deferred") == []


def test_a_pull_request_merged_by_hand_is_never_armed(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    calls = github(
        monkeypatch,
        pulls={3: pull(3, merged=True)},
        issues={11: {"number": 11, "state": "open"}},
    )
    landing.landing_tick(POLICY)
    assert calls["graphql"] == []
    assert audits(db, "merged", "t-1") == [{"pr_number": 3, "armed_by_factory": False}]
    assert audits(db, "issue_closed", "t-1")[0]["closed_by_factory"] is True


def test_a_refused_arming_is_final_and_never_retried(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    calls = github(monkeypatch, pulls={3: pull(3)})

    def refuse(_query, variables):
        calls["graphql"].append(variables)
        raise landing.GraphQLRefused("UNPROCESSABLE", "GitHub refused the mutation")

    monkeypatch.setattr(landing, "github_graphql", refuse)
    landing.landing_tick(POLICY)
    landing.landing_tick(POLICY)
    assert len(calls["graphql"]) == 1
    assert audits(db, "merge_arm_refused", "t-1") == [
        {"pr_number": 3, "reason": "UNPROCESSABLE"}
    ]
    assert audits(db, "merge_armed") == []


def test_a_closed_unmerged_pull_request_stops_the_landing(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    pulls = {3: pull(3)}
    github(monkeypatch, pulls=pulls)
    landing.landing_tick(POLICY)
    pulls[3] = pull(3, state="closed")
    landing.landing_tick(POLICY)
    landing.landing_tick(POLICY)
    assert audits(db, "merge_arm_refused", "t-1") == [
        {"pr_number": 3, "reason": "pull request closed without merging"}
    ]
    assert audits(db, "merged") == []


def test_an_advisory_settlement_is_not_a_delivery(db, monkeypatch):
    delivered(db, "t-1", 11, 3, evidence=False)
    monkeypatch.setattr(
        landing,
        "github_get",
        lambda *_args: pytest.fail("landing read GitHub for an advisory task"),
    )
    landing.landing_tick(POLICY)
    assert audits(db, "merge_armed") == []


def test_a_settlement_older_than_the_window_is_left_alone(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        row.updated_at = NOW - timedelta(hours=landing.LANDING_WINDOW_HOURS + 1)
        session.add(row)
        session.commit()
    monkeypatch.setattr(
        landing,
        "github_get",
        lambda *_args: pytest.fail("landing read GitHub for a stale settlement"),
    )
    landing.landing_tick(POLICY)
    assert audits(db, "merge_armed") == []


def test_a_read_outage_is_audited_by_shape_and_carries_no_response_body(
    db, monkeypatch
):
    delivered(db, "t-1", 11, 3)

    def outage(_repo, _suffix):
        raise httpx.HTTPStatusError(
            "403 rate limited for https://api.github.com/repos/owner/repo/pulls/3",
            request=httpx.Request("GET", "https://api.github.com"),
            response=httpx.Response(403),
        )

    monkeypatch.setattr(landing, "github_get", outage)
    landing.landing_tick(POLICY)
    recorded = audits(db, "landing_error", "t-1")
    assert recorded == [{"stage": "arm", "error": "HTTPStatusError", "status": 403}]
    # The next tick retries rather than giving up, and the throttle keeps the
    # audit to one row an hour while the outage lasts.
    landing.landing_tick(POLICY)
    assert len(audits(db, "landing_error", "t-1")) == 1
    assert audits(db, "merge_arm_refused") == []


def mock_httpx(monkeypatch, handler):
    """Bind one transport into the landing module without touching httpx itself."""
    real = httpx.Client

    def client(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(
        landing, "httpx", SimpleNamespace(Client=client, HTTPError=httpx.HTTPError)
    )


def test_graphql_reads_the_errors_array_as_a_refusal(monkeypatch):
    """GitHub answers a refused mutation with HTTP 200 and an errors array."""
    mock_httpx(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json={
                "data": None,
                "errors": [
                    {
                        "type": "UNPROCESSABLE",
                        "message": "Pull request is in clean status",
                    }
                ],
            },
        ),
    )
    with pytest.raises(landing.GraphQLRefused) as refusal:
        landing.github_graphql("mutation {}", {"pullRequestId": "PR_3"})
    assert refusal.value.code == "UNPROCESSABLE"
    # The audit reason never carries the GitHub message, which can quote
    # repository content back into an operator-facing row.
    assert "clean status" not in str(refusal.value)


def test_graphql_returns_the_data_block_when_the_mutation_takes(monkeypatch):
    mock_httpx(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json={
                "data": {"enablePullRequestAutoMerge": {"pullRequest": {"number": 3}}}
            },
        ),
    )
    result = landing.github_graphql("mutation {}", {"pullRequestId": "PR_3"})
    assert result["enablePullRequestAutoMerge"]["pullRequest"]["number"] == 3


def test_writes_reach_only_the_configured_repository(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"ok": True})

    mock_httpx(monkeypatch, handler)
    landing.github_write("owner/repo", "issues/11", {"state": "closed"}, method="PATCH")
    assert seen == {
        "url": "https://api.github.com/repos/owner/repo/issues/11",
        "method": "PATCH",
    }
    with pytest.raises(ValueError, match="invalid repository"):
        landing.github_write("owner/repo/../other", "issues/11", {})
