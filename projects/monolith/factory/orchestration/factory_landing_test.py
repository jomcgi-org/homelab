"""Landing an approved delivery: arming one merge, then closing its issue."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import factory.orchestration.factory_controls as controls
import factory.orchestration.factory_landing as landing
from factory.orchestration import factory_rollout
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
POLICY = {"repo": "owner/repo", "auto_merge": True}
HEAD = "a" * 40


@pytest.fixture
def db(tmp_path, monkeypatch):
    landing._delivery_cursors.clear()
    monkeypatch.setattr(
        factory_rollout, "verify", lambda *_args: {"verified": True, "applications": []}
    )
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
                SwarmNodeRun,
                FactoryControl,
                FactoryReceipt,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
                FactoryStart,
                FactoryAudit,
            )
        ],
    )
    with Session(engine) as session:
        session.add(
            FactoryControl(
                id="factory",
                actor="migration",
                state="enabled",
                policy_json=json.dumps(POLICY),
            )
        )
        session.commit()
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(landing, "_now", lambda: NOW)
    monkeypatch.setattr(controls, "_now", lambda: NOW)
    monkeypatch.setattr(landing, "_notify_stuck", lambda *_args: None)
    yield engine
    engine.dispose()


def delivered(
    db,
    task_id,
    issue_number,
    pr_number,
    *,
    evidence=True,
    task_class="bug-fix",
    settled=None,
    head=HEAD,
    state="succeeded",
):
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
                state=state,
                task_class=task_class,
                task_id=task_id,
                updated_at=settled or (NOW - timedelta(minutes=5)),
            )
        )
        session.flush()
        session.add(
            FactoryAudit(
                actor="factory:reconciler",
                action="delivery_ready" if state == "landing" else "finish_task",
                task_id=task_id,
                detail_json=json.dumps(
                    {
                        "outcome": "succeeded",
                        "evidence": (
                            {
                                "pr_url": (
                                    f"https://github.com/owner/repo/pull/{pr_number}"
                                ),
                                "head_sha": head,
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


def receipt_state(db, task_id):
    with Session(db) as session:
        return session.exec(
            select(FactoryReceipt.state).where(FactoryReceipt.task_id == task_id)
        ).one()


def pull(
    number,
    *,
    merged=False,
    state="open",
    draft=False,
    armed=False,
    head=HEAD,
    task_id="t-1",
):
    return {
        "number": number,
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "state": "closed" if merged else state,
        "draft": draft,
        "merged": merged,
        "node_id": f"PR_{number}",
        "body": "Closes #11",
        "head": {
            "sha": head,
            "ref": f"factory/{task_id}",
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        "auto_merge": {"merge_method": "REBASE"} if armed else None,
        "merge_commit_sha": "b" * 40 if merged else None,
    }


def github(monkeypatch, *, pulls=None, issues=None, refuse=None):
    """A GitHub that behaves: the mutations move the state the reads return."""
    calls = {"get": [], "list": [], "graphql": [], "write": []}
    pulls = {} if pulls is None else pulls
    issues = {} if issues is None else issues

    def get(_repo, suffix):
        calls["get"].append(suffix)
        number = int(suffix.split("/")[1])
        source = pulls if suffix.startswith("pulls/") else issues
        return dict(source.get(number, {"state": "open", "labels": []}))

    def listing(_repo, suffix):
        calls["list"].append(suffix)
        return [dict(row) for row in pulls.values() if row["state"] == "open"]

    def graphql(query, variables):
        if query == landing._QUEUE_ENTRIES:
            return {
                "nodes": [
                    {
                        "id": node_id,
                        "mergeQueueEntry": pulls.get(
                            int(node_id.removeprefix("PR_")), {}
                        ).get("queue_entry"),
                    }
                    for node_id in variables["ids"]
                ]
            }
        calls["graphql"].append(variables)
        if refuse is not None:
            refuse()
        number = int(variables["pullRequestId"].removeprefix("PR_"))
        if "dequeuePullRequest" in query:
            pulls[number] = {**pulls[number], "queue_entry": None}
            return {"mergeQueueEntry": {"id": f"MQ_{number}"}}
        arming = "enablePullRequestAutoMerge" in query
        pulls[number] = {
            **pulls[number],
            "auto_merge": {"merge_method": "REBASE"} if arming else None,
        }
        return {}

    def write(_repo, suffix, payload, *, method="POST"):
        calls["write"].append((method, suffix, payload))
        return {}

    monkeypatch.setattr(landing, "github_get", get)
    monkeypatch.setattr(landing, "github_list", listing)
    monkeypatch.setattr(landing, "github_graphql", graphql)
    monkeypatch.setattr(landing, "github_write", write)
    return calls


def test_landing_observes_without_mutating_open_pr_when_flags_off(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    calls = github(monkeypatch, pulls={3: pull(3)})
    landing.landing_tick({"repo": "owner/repo"})
    assert "pulls/3" in calls["get"]
    assert calls["write"] == calls["graphql"] == []


def test_missing_publisher_token_refuses_to_arm_when_enabled(db, monkeypatch):
    from factory import review_publisher

    delivered(db, "t-1", 11, 3)
    calls = github(monkeypatch, pulls={3: pull(3)})
    monkeypatch.setenv(review_publisher.PUBLISH_ENABLED_ENV, "true")
    monkeypatch.delenv(review_publisher.PUBLISHER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(
        review_publisher,
        "collect",
        lambda _task_id: review_publisher.ReviewEvidence(
            task_id="t-1",
            repo="owner/repo",
            pr_number=3,
            branch="factory/t-1",
            head_sha=HEAD,
            review_run_id=17,
            review_session_id=11,
            policy_version=1,
            details_url="https://private.jomcgi.dev/agents/session/11",
        ),
    )

    landing.landing_tick(POLICY)

    assert calls["graphql"] == []
    assert audits(db, "review_publish_skipped", "t-1") == [
        {"reason": "publisher_token_missing"}
    ]
    assert audits(db, "merge_arm_refused", "t-1") == [
        {
            "pr_number": 3,
            "reason": "review_not_published",
            "review_reason": "publisher_token_missing",
        }
    ]


def test_review_canary_publishes_while_merge_remains_disabled(db, monkeypatch):
    from factory import review_publisher

    delivered(db, "t-1", 11, 3)
    monkeypatch.setattr(review_publisher, "enabled", lambda: True)
    evidence = review_publisher.ReviewEvidence(
        task_id="t-1",
        repo="owner/repo",
        pr_number=3,
        branch="factory/t-1",
        head_sha=HEAD,
        review_run_id=17,
        review_session_id=11,
        policy_version=1,
        details_url="https://private.jomcgi.dev/agents/session/11",
    )
    monkeypatch.setattr(review_publisher, "collect", lambda _task_id: evidence)
    monkeypatch.setattr(
        review_publisher,
        "publish",
        lambda _evidence: {
            "action": "published",
            "conclusion": "success",
            "check_id": 91,
            "head_sha": HEAD,
            "review_run_id": 17,
        },
    )
    monkeypatch.setattr(
        landing,
        "github_get",
        lambda _repo, suffix: (
            {"labels": []} if suffix.startswith("issues/") else pull(3)
        ),
    )

    landing.landing_tick({"repo": "owner/repo", "auto_merge": False})

    assert audits(db, "review_published", "t-1") == [
        {
            "pr_number": 3,
            "head_sha": HEAD,
            "review_run_id": 17,
            "check_id": 91,
            "conclusion": "success",
        }
    ]
    assert audits(db, "merge_armed") == []


def test_landing_arms_one_merge_and_does_not_arm_it_twice(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    calls = github(monkeypatch, pulls={3: pull(3)})
    landing.landing_tick(POLICY)
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]
    assert audits(db, "merge_armed", "t-1") == [
        {"pr_number": 3, "head_sha": HEAD, "attempt": 1, "merge_method": "rebase"}
    ]
    # A second tick observes the armed pull request; it never arms it again.
    landing.landing_tick(POLICY)
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]
    assert len(audits(db, "merge_armed")) == 1


def test_a_conflicting_delivery_reopens_the_task_without_arming(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    pulls = {
        3: {
            **pull(3),
            "mergeable": False,
            "mergeable_state": "dirty",
        }
    }
    calls = github(monkeypatch, pulls=pulls)

    landing.landing_tick(POLICY)

    assert calls["graphql"] == []
    assert receipt_state(db, "t-1") == "admitted"
    assert audits(db, "landing_recovery_requested", "t-1") == [
        {
            "pr_number": 3,
            "head_sha": HEAD,
            "source": "delivered_pr",
            "reason": "merge_conflict",
            "deadline_at": (NOW + timedelta(hours=1)).isoformat(),
            "run_id_floor": 0,
        }
    ]
    assert audits(db, "merge_armed") == []


def test_landing_never_arms_a_head_newer_than_delivery_approval(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    pulls = {3: pull(3, head="c" * 40)}
    calls = github(monkeypatch, pulls=pulls)

    landing.landing_tick(POLICY)

    assert calls["graphql"] == []
    assert audits(db, "merge_arm_refused", "t-1") == [
        {
            "pr_number": 3,
            "reason": "head_moved",
            "approved_head_sha": HEAD,
            "head_sha": "c" * 40,
        }
    ]


def test_landing_never_arms_without_an_approved_head(db, monkeypatch):
    delivered(db, "t-1", 11, 3, head=None)
    calls = github(monkeypatch, pulls={3: pull(3)})

    landing.landing_tick(POLICY)

    assert calls["graphql"] == []
    assert audits(db, "merge_arm_refused", "t-1") == [
        {"pr_number": 3, "reason": "approved_head_missing"}
    ]


def test_landing_never_arms_without_a_current_pull_request_head(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    current = pull(3)
    current["head"] = {"ref": "factory/t-3"}
    calls = github(monkeypatch, pulls={3: current})

    landing.landing_tick(POLICY)

    assert calls["graphql"] == []
    assert audits(db, "merge_arm_refused", "t-1") == [
        {
            "pr_number": 3,
            "reason": "pull_request_head_missing",
            "approved_head_sha": HEAD,
        }
    ]


def test_landing_defers_every_waiting_delivery_while_one_is_armed(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    delivered(db, "t-2", 12, 4)
    delivered(db, "t-3", 13, 5)
    calls = github(monkeypatch, pulls={3: pull(3), 4: pull(4), 5: pull(5)})
    landing.landing_tick(POLICY)
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]
    # The merge queue ejects everything behind a failed candidate, so exactly
    # one factory pull request is ever armed, and BOTH waiting deliveries say
    # so rather than only the one at the front.
    assert audits(db, "merge_deferred", "t-2") == [
        {"pr_number": 4, "blocked_by_pr": 3, "blocked_by_task_id": "t-1"}
    ]
    assert audits(db, "merge_deferred", "t-3") == [
        {"pr_number": 5, "blocked_by_pr": 3, "blocked_by_task_id": "t-1"}
    ]
    # The deferral is recorded once per blocker, not once per tick.
    landing.landing_tick(POLICY)
    assert len(audits(db, "merge_deferred", "t-2")) == 1
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]


def test_a_merge_records_the_merge_and_closes_the_issue(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    pulls = {3: pull(3)}
    issues = {11: {"number": 11, "state": "open"}}
    calls = github(monkeypatch, pulls=pulls, issues=issues)
    landing.landing_tick(POLICY)
    pulls[3] = pull(3, merged=True)
    landing.landing_tick(POLICY)
    assert audits(db, "merged", "t-1") == [
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
    # The comment says what was observed. It must not claim the body lacked a
    # closing keyword, which is one cause of an open issue and not the only one.
    body = calls["write"][0][2]["body"]
    assert "#3" in body and "verifying publication" in body
    assert "keyword" not in body
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


def test_a_pull_request_merged_by_hand_is_never_armed(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    calls = github(
        monkeypatch,
        pulls={3: pull(3, merged=True)},
        issues={11: {"number": 11, "state": "open"}},
    )
    landing.landing_tick(POLICY)
    assert calls["graphql"] == []
    assert audits(db, "merged", "t-1") == [
        {"pr_number": 3, "armed_by_factory": False, "merge_commit_sha": "b" * 40}
    ]
    assert audits(db, "issue_closed", "t-1")[0]["closed_by_factory"] is True


def test_a_refused_arming_is_final_and_never_retried(db, monkeypatch):
    delivered(db, "t-1", 11, 3)

    def refuse():
        raise landing.GraphQLRefused("UNPROCESSABLE", "GitHub refused the mutation")

    calls = github(monkeypatch, pulls={3: pull(3)}, refuse=refuse)
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
    pulls[3] = pull(3, state="closed", armed=True)
    landing.landing_tick(POLICY)
    landing.landing_tick(POLICY)
    assert audits(db, "merge_arm_refused", "t-1") == [
        {"pr_number": 3, "reason": "pull request closed without merging"}
    ]
    assert audits(db, "merged") == []


def test_ejection_requests_assessment_and_releases_holder(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    delivered(db, "t-2", 12, 4)
    pulls = {3: pull(3), 4: pull(4)}
    calls = github(monkeypatch, pulls=pulls)
    landing.landing_tick(POLICY)
    pulls[3]["auto_merge"] = None
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-1") == "admitted"
    assert (
        audits(db, "landing_recovery_requested", "t-1")[0]["reason"] == "queue_ejection"
    )
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}, {"pullRequestId": "PR_4"}]
    landing.landing_tick(POLICY)
    assert len(audits(db, "landing_recovery_requested", "t-1")) == 1


def reviewed_recovery(db, head=HEAD):
    """Model exact-head recovery work and its fresh independent review."""
    with Session(db) as session:
        event = session.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == "t-1",
                FactoryAudit.action == "landing_recovery_requested",
            )
            .order_by(FactoryAudit.id.desc())
        ).first()
        session.add(
            FactoryAudit(
                actor="test",
                task_id="t-1",
                action="landing_recovery_round",
                detail_json=json.dumps({"request_id": event.id}),
            )
        )
        session.add(
            SwarmNodeRun(
                task_id="t-1",
                node_key=f"integrate_recovery_{event.id}",
                attempt=1,
                status="succeeded",
                session_id=11,
                head_sha=head,
                outcome_json=json.dumps(
                    {
                        "value": {
                            "status": "complete",
                            "pr_number": 3,
                            "head_sha": head,
                        }
                    }
                ),
            )
        )
        session.add(
            SwarmNodeRun(
                task_id="t-1",
                node_key=f"review_recovery_{event.id}",
                attempt=1,
                status="succeeded",
                session_id=12,
                head_sha=head,
                outcome_json=json.dumps(
                    {
                        "value": {
                            "verdict": "approve",
                            "pr_number": 3,
                            "head_sha": head,
                        }
                    }
                ),
            )
        )
        session.commit()


def test_recovery_is_bounded_across_new_settlements(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    pulls = {3: pull(3)}
    warned = []
    github(monkeypatch, pulls=pulls)
    monkeypatch.setattr(landing, "_notify_stuck", lambda *args: warned.append(args))
    for ordinal in range(3):
        landing.landing_tick(POLICY)
        pulls[3]["auto_merge"] = None
        landing.landing_tick(POLICY)
        if ordinal < 2:
            assert receipt_state(db, "t-1") == "admitted"
            reviewed_recovery(db)
            assert controls.finish_task(
                "t-1",
                "succeeded",
                "test",
                evidence={
                    "pr_url": "https://github.com/owner/repo/pull/3",
                    "head_sha": HEAD,
                    "review_session_id": 12,
                    "state": "ready_for_review",
                },
            )["ok"]
    assert len(audits(db, "landing_recovery_requested", "t-1")) == 2
    assert (
        audits(db, "merge_arm_refused", "t-1")[-1]["reason"]
        == "landing_recovery_exhausted"
    )
    landing.landing_tick(POLICY)
    assert warned == [("t-1", 3)]


def test_recovery_respects_delivery_capacity(db, monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "1")
    delivered(db, "t-1", 11, 3)
    delivered(db, "t-2", 12, 4)
    with Session(db) as session:
        row = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == "t-2")
        ).one()
        row.state = "admitted"
        session.add(row)
        session.commit()
    calls = github(monkeypatch, pulls={3: {**pull(3), "mergeable": False}})
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-1") == "succeeded"
    assert audits(db, "landing_recovery_requested", "t-1") == []
    assert calls["graphql"] == []


def test_conflict_disarms_holder_and_advances_next_delivery(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    delivered(db, "t-2", 12, 4)
    pulls = {3: pull(3), 4: pull(4)}
    calls = github(monkeypatch, pulls=pulls)
    landing.landing_tick(POLICY)
    pulls[3]["mergeable"] = False
    landing.landing_tick(POLICY)
    assert pulls[3]["auto_merge"] is None
    assert pulls[4]["auto_merge"] is not None
    assert receipt_state(db, "t-1") == "admitted"
    assert audits(db, "merge_arm_refused", "t-1") == []
    assert audits(db, "merge_ejected", "t-1") == []
    assert calls["graphql"] == [
        {"pullRequestId": "PR_3"},
        {"pullRequestId": "PR_3"},
        {"pullRequestId": "PR_4"},
    ]


def test_failed_conflict_disarm_keeps_slot_until_retry_succeeds(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    delivered(db, "t-2", 12, 4)
    pulls = {3: pull(3), 4: pull(4)}
    github(monkeypatch, pulls=pulls)
    landing.landing_tick(POLICY)
    pulls[3]["mergeable"] = False
    original = landing.github_graphql

    def refused(_query, _variables):
        raise landing.GraphQLRefused("FORBIDDEN", "test refusal")

    monkeypatch.setattr(landing, "github_graphql", refused)
    landing.landing_tick(POLICY)
    assert pulls[3]["auto_merge"] is not None
    assert pulls[4]["auto_merge"] is None
    assert audits(db, "merge_arm_refused", "t-1") == []
    monkeypatch.setattr(landing, "github_graphql", original)
    landing.landing_tick(POLICY)
    assert pulls[3]["auto_merge"] is None
    assert pulls[4]["auto_merge"] is not None


@pytest.mark.parametrize("state", ["succeeded", "landing"])
def test_a_merge_conflict_ejection_reopens_for_correction_instead_of_rearming(
    db, monkeypatch, state
):
    delivered(db, "t-1", 11, 3, state=state)
    pulls = {3: pull(3)}
    calls = github(monkeypatch, pulls=pulls)
    landing.landing_tick(POLICY)
    pulls[3] = {
        **pulls[3],
        "auto_merge": None,
        "mergeable": False,
        "mergeable_state": "dirty",
    }

    landing.landing_tick(POLICY)
    landing.landing_tick(POLICY)

    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]
    assert receipt_state(db, "t-1") == "admitted"
    assert audits(db, "merge_ejected", "t-1") == []
    assert audits(db, "landing_recovery_requested", "t-1") == [
        {
            "pr_number": 3,
            "head_sha": HEAD,
            "source": "merge_queue",
            "reason": "merge_conflict",
            "deadline_at": (NOW + timedelta(hours=1)).isoformat(),
            "run_id_floor": 0,
        }
    ]
    assert audits(db, "merge_arm_refused", "t-1") == []


def test_a_corrected_merge_conflict_rearms_at_the_newly_approved_head(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    pulls = {3: pull(3)}
    calls = github(monkeypatch, pulls=pulls)
    landing.landing_tick(POLICY)
    pulls[3] = {
        **pulls[3],
        "auto_merge": None,
        "mergeable": False,
        "mergeable_state": "dirty",
    }
    landing.landing_tick(POLICY)

    corrected_head = "c" * 40
    reviewed_recovery(db, corrected_head)
    assert controls.finish_task(
        "t-1",
        "succeeded",
        "test",
        evidence={
            "pr_url": "https://github.com/owner/repo/pull/3",
            "head_sha": corrected_head,
            "review_session_id": 12,
            "reviewer_model": "opus",
            "state": "ready_for_review",
        },
    )["ok"]
    pulls[3] = pull(3, head=corrected_head)

    landing.landing_tick(POLICY)

    assert calls["graphql"] == [
        {"pullRequestId": "PR_3"},
        {"pullRequestId": "PR_3"},
    ]
    assert audits(db, "merge_armed", "t-1") == [
        {
            "pr_number": 3,
            "head_sha": HEAD,
            "attempt": 1,
            "merge_method": "rebase",
        },
        {
            "pr_number": 3,
            "head_sha": corrected_head,
            "attempt": 1,
            "merge_method": "rebase",
        },
    ]
    assert audits(db, "merge_arm_refused", "t-1") == []


def test_historical_refusal_is_recovered_once_and_rearmed_after_review(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    landing._record("t-1", "merge_arm_refused", pr_number=3, reason="merge_conflict")
    pulls = {3: {**pull(3), "mergeable": False}}
    calls = github(monkeypatch, pulls=pulls)
    landing.landing_tick(POLICY)
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-1") == "admitted"
    assert len(audits(db, "landing_recovery_requested", "t-1")) == 1
    assert calls["graphql"] == []
    new_head = "c" * 40
    reviewed_recovery(db, new_head)
    assert controls.finish_task(
        "t-1",
        "succeeded",
        "test",
        evidence={
            "pr_url": "https://github.com/owner/repo/pull/3",
            "head_sha": new_head,
            "review_session_id": 12,
            "state": "ready_for_review",
        },
    )["ok"]
    pulls[3] = pull(3, head=new_head)
    landing.landing_tick(POLICY)
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]
    assert audits(db, "merge_armed", "t-1")[-1]["head_sha"] == new_head


def test_a_head_that_moves_under_an_armed_pull_request_is_disarmed(db, monkeypatch):
    """What the review approved is not what would merge, so it goes to a human."""
    delivered(db, "t-1", 11, 3)
    pulls = {3: pull(3)}
    calls = github(monkeypatch, pulls=pulls)
    landing.landing_tick(POLICY)
    pulls[3] = {**pulls[3], "head": {"sha": "c" * 40, "ref": "factory/t-3"}}
    landing.landing_tick(POLICY)
    assert audits(db, "merge_arm_refused", "t-1") == [
        {
            "pr_number": 3,
            "reason": "head_moved",
            "armed_head_sha": HEAD,
            "head_sha": "c" * 40,
        }
    ]
    # Auto-merge is actually turned off again, not merely recorded.
    assert calls["graphql"][-1] == {"pullRequestId": "PR_3"}
    assert pulls[3]["auto_merge"] is None
    assert audits(db, "merged") == []


def test_a_pull_request_an_operator_armed_counts_as_the_holder(db, monkeypatch):
    """The audit trail only knows what this lane did; the queue does not care."""
    delivered(db, "t-1", 11, 3)
    calls = github(
        monkeypatch,
        pulls={3: pull(3), 9: pull(9, armed=True)},
    )
    landing.landing_tick(POLICY)
    assert calls["graphql"] == []
    assert audits(db, "merge_deferred", "t-1") == [
        {"pr_number": 3, "blocked_by_pr": 9, "blocked_by_task_id": None}
    ]


@pytest.mark.parametrize("armed", [False, True])
def test_holder_on_second_page_controls_whether_landing_arms(db, monkeypatch, armed):
    delivered(db, "t-1", 11, 3)
    calls = github(monkeypatch, pulls={3: pull(3)})
    pages = []

    def listing(_repo, suffix):
        pages.append(suffix)
        if suffix.endswith("page=1"):
            return [pull(number) for number in range(100, 150)]
        return [pull(999, armed=armed)]

    monkeypatch.setattr(landing, "github_list", listing)
    landing.landing_tick(POLICY)
    assert pages == [
        "pulls?state=open&sort=created&direction=asc&per_page=50&page=1",
        "pulls?state=open&sort=created&direction=asc&per_page=50&page=2",
    ]
    if armed:
        assert calls["graphql"] == []
        assert audits(db, "merge_deferred", "t-1") == [
            {"pr_number": 3, "blocked_by_pr": 999, "blocked_by_task_id": None}
        ]
    else:
        assert calls["graphql"] == [{"pullRequestId": "PR_3"}]


@pytest.mark.parametrize("failure", ["page_limit", "read_error"])
def test_incomplete_holder_pagination_never_arms(db, monkeypatch, failure):
    delivered(db, "t-1", 11, 3)
    calls = github(monkeypatch, pulls={3: pull(3)})
    pages = []

    def listing(_repo, suffix):
        pages.append(suffix)
        if failure == "read_error" and len(pages) == 2:
            raise ValueError("GitHub response exceeds factory limit")
        return [pull(number) for number in range(100, 150)]

    monkeypatch.setattr(landing, "github_list", listing)
    landing.landing_tick(POLICY)
    assert len(pages) == (5 if failure == "page_limit" else 2)
    assert calls["graphql"] == []
    assert audits(db, "landing_error", "t-1") == [
        {"stage": "holder", "error": "ValueError", "status": None}
    ]


def test_an_armed_pull_request_outside_the_factory_branches_is_not_a_holder(
    db, monkeypatch
):
    delivered(db, "t-1", 11, 3)
    human = pull(9, armed=True)
    human["head"] = {"sha": "d" * 40, "ref": "fix/something"}
    calls = github(monkeypatch, pulls={3: pull(3), 9: human})
    landing.landing_tick(POLICY)
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]
    assert audits(db, "merge_deferred") == []


def test_an_unreadable_holder_check_never_arms_a_second_pull_request(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    calls = github(monkeypatch, pulls={3: pull(3)})

    def outage(_repo, _suffix):
        raise httpx.HTTPStatusError(
            "403",
            request=httpx.Request("GET", "https://api.github.com"),
            response=httpx.Response(403),
        )

    monkeypatch.setattr(landing, "github_list", outage)
    landing.landing_tick(POLICY)
    assert calls["graphql"] == []
    assert audits(db, "landing_error", "t-1") == [
        {"stage": "holder", "error": "HTTPStatusError", "status": 403}
    ]


def test_a_burst_of_newer_settlements_never_evicts_an_armed_delivery(db, monkeypatch):
    """Selection follows landing state, never recency.

    Taking the newest receipts of any class let an armed but unmerged delivery
    fall out of the batch: never observed, never merged, the issue never
    closed, and the holder reading as absent so a second merge was armed.
    """
    delivered(db, "t-1", 11, 3)
    pulls = {3: pull(3)}
    issues = {11: {"number": 11, "state": "open"}}
    github(monkeypatch, pulls=pulls, issues=issues)
    landing.landing_tick(POLICY)
    assert audits(db, "merge_armed", "t-1")
    # Thirty newer settlements, advisory and delivered alike, more than any
    # batch of recent receipts would hold.
    for index in range(30):
        delivered(
            db,
            f"t-adv-{index}",
            100 + index,
            None,
            evidence=False,
            task_class="refine",
            settled=NOW,
        )
    pulls[3] = pull(3, merged=True)
    landing.landing_tick(POLICY)
    assert audits(db, "merged", "t-1")
    assert audits(db, "issue_closed", "t-1")


def test_a_terminal_delivery_leaves_the_batch(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    delivered(db, "t-2", 12, 4)
    pulls = {3: pull(3, merged=True), 4: pull(4)}
    calls = github(
        monkeypatch,
        pulls=pulls,
        issues={11: {"number": 11, "state": "closed"}},
    )
    landing.landing_tick(POLICY)
    assert audits(db, "issue_closed", "t-1")
    reads = len(calls["get"])
    landing.landing_tick(POLICY)
    # The landed delivery is not read again; only the one still working is.
    assert [suffix for suffix in calls["get"][reads:]] == [
        "issues/12",
        "pulls/4",
        "pulls/4",
    ]


def test_an_advisory_settlement_is_not_a_delivery(db, monkeypatch):
    delivered(db, "t-1", 11, 3, evidence=False, task_class="refine")
    monkeypatch.setattr(
        landing,
        "github_get",
        lambda *_args: pytest.fail("landing read GitHub for an advisory task"),
    )
    monkeypatch.setattr(
        landing,
        "github_list",
        lambda *_args: pytest.fail("landing listed GitHub for an advisory task"),
    )
    landing.landing_tick(POLICY)
    assert audits(db, "merge_armed") == []


def test_a_settlement_older_than_the_window_is_left_alone(db, monkeypatch):
    delivered(
        db,
        "t-1",
        11,
        3,
        settled=NOW - timedelta(hours=landing.LANDING_WINDOW_HOURS + 1),
    )
    monkeypatch.setattr(
        landing,
        "github_get",
        lambda *_args: pytest.fail("landing read GitHub for a stale settlement"),
    )
    monkeypatch.setattr(landing, "github_list", lambda *_args: [])
    landing.landing_tick(POLICY)
    assert audits(db, "merge_armed") == []


def test_a_delivery_the_lane_already_armed_outlives_the_window(db, monkeypatch):
    """The window keeps history out; it must never strand live work."""
    delivered(db, "t-1", 11, 3)
    pulls = {3: pull(3)}
    github(monkeypatch, pulls=pulls, issues={11: {"number": 11, "state": "closed"}})
    landing.landing_tick(POLICY)
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        row.updated_at = NOW - timedelta(hours=landing.LANDING_WINDOW_HOURS + 1)
        session.add(row)
        session.commit()
    pulls[3] = pull(3, merged=True)
    landing.landing_tick(POLICY)
    assert audits(db, "merged", "t-1")


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
    monkeypatch.setattr(landing, "github_list", lambda *_args: [])
    landing.landing_tick(POLICY)
    assert audits(db, "landing_error", "t-1") == [
        {"stage": "human_ownership", "error": "HTTPStatusError", "status": 403}
    ]
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


def test_a_repeatable_action_is_never_written_through_the_once_only_fence():
    """The fence freezes a state machine that has to move more than once."""
    with pytest.raises(ValueError, match="repeatable"):
        landing._record("t-1", "merge_armed", pr_number=3)


def test_queued_holder_without_auto_merge_is_not_ejected(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    delivered(db, "t-2", 12, 4)
    pulls = {3: pull(3), 4: pull(4)}
    calls = github(monkeypatch, pulls=pulls, issues={11: {"state": "closed"}})
    landing.landing_tick(POLICY)
    pulls[3].update(auto_merge=None, queue_entry={"id": "MQ_3"})
    landing.landing_tick(POLICY)
    landing.landing_tick(POLICY)
    assert audits(db, "merge_ejected") == []
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]
    pulls[3] = pull(3, merged=True)
    landing.landing_tick(POLICY)
    assert audits(db, "merged", "t-1")[0]["armed_by_factory"] is True
    assert calls["graphql"][-1] == {"pullRequestId": "PR_4"}


def test_external_queue_entry_holds_the_lane(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    queued = {**pull(9), "queue_entry": {"id": "MQ_9"}}
    calls = github(monkeypatch, pulls={3: pull(3), 9: queued})
    landing.landing_tick(POLICY)
    assert calls["graphql"] == []
    assert audits(db, "merge_deferred", "t-1") == [
        {"pr_number": 3, "blocked_by_pr": 9, "blocked_by_task_id": None}
    ]


@pytest.mark.parametrize("armed", [False, True])
def test_unreadable_queue_membership_never_releases_slot(db, monkeypatch, armed):
    delivered(db, "t-1", 11, 3)
    delivered(db, "t-2", 12, 4)
    pulls = {3: pull(3), 4: pull(4)}
    calls = github(monkeypatch, pulls=pulls)
    if armed:
        landing.landing_tick(POLICY)
        pulls[3]["auto_merge"] = None
    original = landing.github_graphql

    def unavailable(query, variables):
        if query == landing._QUEUE_ENTRIES:
            return {"nodes": [None for _ in variables["ids"]]}
        return original(query, variables)

    monkeypatch.setattr(landing, "github_graphql", unavailable)
    landing.landing_tick(POLICY)
    assert len(calls["graphql"]) == int(armed)
    assert audits(db, "merge_ejected") == []
    assert audits(db, "landing_error")


def test_queue_entry_on_second_page_holds_the_lane(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    queued = {**pull(999), "queue_entry": {"id": "MQ_999"}}
    calls = github(monkeypatch, pulls={3: pull(3), 999: queued})

    def listing(_repo, suffix):
        if suffix.endswith("page=1"):
            return [pull(number) for number in range(100, 150)]
        return [queued]

    monkeypatch.setattr(landing, "github_list", listing)
    landing.landing_tick(POLICY)
    assert calls["graphql"] == []
    assert audits(db, "merge_deferred", "t-1")[0]["blocked_by_pr"] == 999


@pytest.mark.parametrize("status", ["reserved", "uncertain"])
def test_recovery_never_reopens_unresolved_execution(db, monkeypatch, status):
    delivered(db, "t-1", 11, 3)
    with Session(db) as session:
        session.add(
            FactoryStart(
                task_id="t-1",
                start_key="pending",
                actor="test",
                model="astra",
                max_cost_usd=1,
                status=status,
            )
        )
        session.commit()
    result = controls.request_landing_recovery("t-1", 3, HEAD, "merge_queue", "test")
    assert result == {"ok": False, "reason": "unresolved_execution"}
    assert receipt_state(db, "t-1") == "succeeded"


def test_capacity_blocked_recovery_does_not_block_another_ready_pr(db, monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "1")
    for index in range(1, 4):
        delivered(db, f"t-{index}", 10 + index, 2 + index)
    with Session(db) as session:
        row = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == "t-3")
        ).one()
        row.state = "admitted"
        session.add(row)
        session.commit()
    calls = github(monkeypatch, pulls={3: {**pull(3), "mergeable": False}, 4: pull(4)})
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-1") == "succeeded"
    assert calls["graphql"] == [{"pullRequestId": "PR_4"}]


def test_conflict_observation_never_starts_repair_while_pr_is_queued(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    pulls = {3: pull(3)}
    calls = github(monkeypatch, pulls=pulls)
    landing.landing_tick(POLICY)
    pulls[3].update(auto_merge=None, mergeable=False, queue_entry={"id": "MQ_3"})
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-1") == "succeeded"
    assert audits(db, "landing_recovery_requested", "t-1") == []
    assert calls["graphql"] == [{"pullRequestId": "PR_3"}]


def test_historical_changed_head_is_skipped_once_without_blocking_next_refusal(
    db, monkeypatch
):
    for index in (1, 2):
        delivered(db, f"t-{index}", 10 + index, 2 + index)
        landing._record(
            f"t-{index}",
            "merge_arm_refused",
            pr_number=2 + index,
            reason="merge_conflict",
        )
    calls = github(
        monkeypatch,
        pulls={3: pull(3, head="d" * 40), 4: {**pull(4), "mergeable": False}},
    )
    landing.landing_tick(POLICY)
    assert calls["get"][:2] == ["issues/11", "pulls/3"]
    assert audits(db, "landing_recovery_skipped", "t-1")[0]["reason"] == "head_moved"
    landing.landing_tick(POLICY)
    assert "pulls/4" in calls["get"]
    assert receipt_state(db, "t-2") == "admitted"


def test_incomplete_recovery_resumes_without_new_episode_or_arming(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    assert controls.request_landing_recovery("t-1", 3, HEAD, "merge_queue", "test")[
        "ok"
    ]
    original = audits(db, "landing_recovery_requested")
    # Reproduce the old conductor replay: it settled without any recovery round.
    with Session(db) as session:
        row = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == "t-1")
        ).one()
        row.state = "succeeded"
        session.add(row)
        session.add(
            FactoryAudit(
                actor="old-conductor",
                task_id="t-1",
                action="finish_task",
                detail_json=json.dumps(
                    {
                        "outcome": "succeeded",
                        "evidence": {
                            "pr_url": "https://github.com/owner/repo/pull/3",
                            "head_sha": HEAD,
                        },
                    }
                ),
            )
        )
        session.commit()
    calls = github(monkeypatch, pulls={3: pull(3)})
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-1") == "admitted"
    assert audits(db, "landing_recovery_requested") == original
    assert calls["graphql"] == []


def test_repository_delivery_lands_without_closing_live_acceptance(db, monkeypatch):
    """#6208 ends landing after merge but preserves outstanding operational checks."""
    delivered(db, "t-live", 7, 10)
    with Session(db) as session:
        row = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == "t-live")
        ).one()
        row.direction_json = json.dumps(
            {
                "conductor_gates": [
                    {"kind": "live_validation", "live_checks": ["Verify live flows"]}
                ]
            }
        )
        session.add(row)
        session.commit()
    monkeypatch.setattr(
        landing,
        "github_write",
        lambda *a, **kw: pytest.fail("operational issue must remain open"),
    )
    landing._record("t-live", "merged", pr_number=10, merge_commit_sha="b" * 40)
    item = landing._deliveries(POLICY)[0]
    landing._close_issue("owner/repo", item)
    assert item["closed"] is True
    assert not landing._deliveries(POLICY)
    with Session(db) as session:
        audit = session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "repository_delivery_complete"
            )
        ).one()
        assert json.loads(audit.detail_json)["operational_acceptance_pending"] is True


@pytest.mark.parametrize("state", ["succeeded", "landing"])
def test_merge_waits_for_rollout_and_retries_after_bounded_observation(
    db, monkeypatch, state
):
    delivered(db, "t-wait", 11, 3, state=state)
    calls = github(
        monkeypatch,
        pulls={3: pull(3, merged=True, task_id="t-wait")},
        issues={11: {"number": 11, "state": "open"}},
    )
    observations = []

    def pending(repo, merge_sha, pr_number):
        observations.append((repo, merge_sha, pr_number))
        return {"verified": False, "reason": "published_chart_not_deployed"}

    monkeypatch.setattr(factory_rollout, "verify", pending)
    landing.landing_tick(POLICY)
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-wait") == state
    assert observations == [("owner/repo", "b" * 40, 3)]
    assert len(audits(db, "rollout_observed")) == 1
    assert not audits(db, "issue_closed")
    assert not calls["write"]
    assert landing._deliveries(POLICY)[0]["merged"] is True
    monkeypatch.setattr(landing, "_now", lambda: NOW + timedelta(seconds=61))
    monkeypatch.setattr(
        factory_rollout,
        "verify",
        lambda *_: {"verified": True, "merge_commit_sha": "b" * 40, "applications": []},
    )
    landing.landing_tick(POLICY)
    assert len(audits(db, "rollout_verified")) == 1
    assert len(audits(db, "issue_closed")) == 1
    assert receipt_state(db, "t-wait") == "succeeded"
    if state == "landing":
        with Session(db) as session:
            assert session.get(SwarmTask, "t-wait").settled_at is not None
    landing.landing_tick(POLICY)
    assert len(audits(db, "rollout_verified")) == 1


def test_rollout_passes_pr_number_and_audits_blocker_identity(db, monkeypatch):
    delivered(db, "t-scope", 11, 3)
    landing._record("t-scope", "merged", pr_number=3, merge_commit_sha="b" * 40)
    item = landing._deliveries(POLICY)[0]
    calls = []

    def verify(*args):
        calls.append(args)
        return {
            "verified": False,
            "reason": "application_not_healthy_and_synced",
            "application": "embervm",
            "resource": "Deployment/embervm/backend",
        }

    monkeypatch.setattr(factory_rollout, "verify", verify)
    assert landing._verify_rollout("owner/repo", item) is False
    assert calls == [("owner/repo", "b" * 40, 3)]
    observed = audits(db, "rollout_observed", "t-scope")[0]
    assert observed["application"] == "embervm"
    assert observed["resource"] == "Deployment/embervm/backend"


def test_rollout_wait_bounds_are_pinned():
    assert landing.ROLLOUT_WAIT_REPORT_MINUTES == 30
    assert landing.ROLLOUT_ESCALATE_MINUTES == 60


def merged_delivery(db, task_id="t-wait", *, merged_at=NOW):
    delivered(db, task_id, 11, 3)
    with Session(db) as session:
        session.add(
            FactoryAudit(
                actor="test",
                action="merged",
                task_id=task_id,
                created_at=merged_at,
                detail_json=json.dumps({"pr_number": 3, "merge_commit_sha": "b" * 40}),
            )
        )
        session.commit()
    return landing._deliveries(POLICY)[0]


def pending_rollout(monkeypatch):
    monkeypatch.setattr(
        factory_rollout,
        "verify",
        lambda *_: {
            "verified": False,
            "reason": "application_not_healthy_and_synced",
            "application": "embervm",
            "resource": "Deployment/embervm/backend",
        },
    )


def test_deliveries_use_latest_merge_audit_timestamp_without_changing_detail(db):
    merged_delivery(db, merged_at=NOW - timedelta(minutes=10))
    with Session(db) as session:
        session.add(
            FactoryAudit(
                actor="test",
                action="merged",
                task_id="t-wait",
                created_at=NOW,
                detail_json=json.dumps({"pr_number": 3, "merge_commit_sha": "c" * 40}),
            )
        )
        session.commit()
    item = landing._deliveries(POLICY)[0]
    assert item["merged_at"] == NOW
    assert item["merge_commit_sha"] == "c" * 40
    assert audits(db, "merged")[-1] == {"pr_number": 3, "merge_commit_sha": "c" * 40}


def test_rollout_wait_reports_once_and_notifies_once_across_ticks(db, monkeypatch):
    from factory.orchestration import factory_conductor as conductor

    merged_delivery(db)
    pending_rollout(monkeypatch)
    calls = []

    def notify(task_id, message, *, kind):
        calls.append((task_id, message, kind))
        return True

    monkeypatch.setattr(conductor, "_notify_person_once", notify)
    for minute in (29, 30, 59, 60, 61, 62, 63):
        monkeypatch.setattr(
            landing, "_now", lambda minute=minute: NOW + timedelta(minutes=minute)
        )
        assert not landing._verify_rollout("owner/repo", landing._deliveries(POLICY)[0])
        assert len(audits(db, "rollout_wait_reported")) == (0 if minute == 29 else 1)
        assert len(calls) == (0 if minute < 60 else 1)
    assert audits(db, "rollout_wait_reported") == [
        {
            "pr_number": 3,
            "issue_number": 11,
            "merged_at": NOW.isoformat(),
            "waited_minutes": 30,
            "reason": "application_not_healthy_and_synced",
            "application": "embervm",
            "resource": "Deployment/embervm/backend",
        }
    ]
    task_id, message, kind = calls[0]
    assert task_id == "t-wait" and kind == "rollout"
    for literal in (
        "PR #3",
        "issue #11",
        "60 minutes",
        "embervm",
        "Deployment/embervm/backend",
        "application_not_healthy_and_synced",
        "Landing keeps polling",
    ):
        assert literal in message
    assert len(audits(db, "rollout_observed")) == 7
    monkeypatch.setattr(landing, "_now", lambda: NOW + timedelta(minutes=90))
    monkeypatch.setattr(factory_rollout, "verify", lambda *_: {"verified": True})
    assert landing._verify_rollout("owner/repo", landing._deliveries(POLICY)[0])
    assert len(audits(db, "rollout_verified")) == 1
    assert len(audits(db, "rollout_wait_reported")) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("verified,unknown", [(True, False), (False, True)])
def test_verified_or_unknown_merge_time_never_reports_or_pages(
    db, monkeypatch, verified, unknown
):
    from factory.orchestration import factory_conductor as conductor

    item = merged_delivery(db)
    if unknown:
        item["merged_at"] = None
    monkeypatch.setattr(landing, "_now", lambda: NOW + timedelta(minutes=90))
    monkeypatch.setattr(factory_rollout, "verify", lambda *_: {"verified": verified})
    monkeypatch.setattr(
        conductor, "_notify_person_once", lambda *_a, **_k: pytest.fail("must not page")
    )
    assert landing._verify_rollout("owner/repo", item) == verified
    assert not audits(db, "rollout_wait_reported")


def test_rollout_page_real_notification_dedupe_and_failed_send_retry(db, monkeypatch):
    import agent.api
    from factory.orchestration import factory_conductor as conductor

    monkeypatch.setattr(conductor, "get_engine", lambda: db)
    merged_delivery(db)
    pending_rollout(monkeypatch)
    monkeypatch.setenv("FACTORY_NOTIFY_DIGEST_ENABLED", "true")
    sent = []

    async def notify(message, *, level):
        sent.append((message, level))
        if len(sent) == 1:
            raise RuntimeError("retry this send")

    monkeypatch.setattr(agent.api, "notify", notify)
    for minute in (60, 61, 62, 63):
        monkeypatch.setattr(
            landing, "_now", lambda minute=minute: NOW + timedelta(minutes=minute)
        )
        assert not landing._verify_rollout("owner/repo", landing._deliveries(POLICY)[0])
    assert len(sent) == 2 and sent[-1][1] == "warn"
    assert len(audits(db, "task_needs_person_notified")) == 1
    assert conductor._notify_person_once("t-wait", "duplicate", kind="rollout")
    assert len(sent) == 2
    assert len(audits(db, "rollout_wait_reported")) == 1


def test_unavailable_rollout_evidence_keeps_operational_acceptance_pending(
    db, monkeypatch
):
    delivered(db, "t-unavailable", 11, 3)
    github(
        monkeypatch,
        pulls={3: pull(3, merged=True, task_id="t-unavailable")},
        issues={11: {"number": 11, "state": "open"}},
    )
    monkeypatch.setattr(
        factory_rollout,
        "verify",
        lambda *_: {
            "verified": False,
            "reason": "observation_unavailable",
            "error_type": "TimeoutError",
        },
    )
    landing.landing_tick(POLICY)
    assert not audits(db, "rollout_verified")
    assert not audits(db, "issue_closed")
    assert not audits(db, "repository_delivery_complete")
    assert landing._deliveries(POLICY)


def test_new_pending_delivery_does_not_age_out_or_lose_verified_settlement(
    db, monkeypatch
):
    delivered(
        db, "t-old-pending", 11, 3, state="landing", settled=NOW - timedelta(days=30)
    )
    item = landing._deliveries(POLICY)[0]
    assert item["task_id"] == "t-old-pending"
    github(
        monkeypatch,
        pulls={3: pull(3, merged=True)},
        issues={11: {"number": 11, "state": "open"}},
    )
    # Simulate a process exit after the proof committed but before settlement.
    landing._record("t-old-pending", "merged", pr_number=3, merge_commit_sha="b" * 40)
    landing._record(
        "t-old-pending",
        "rollout_verified",
        verified=True,
        pr_number=3,
        approved_head_sha=HEAD,
    )
    monkeypatch.setattr(
        factory_rollout, "verify", lambda *_: pytest.fail("proof already durable")
    )
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-old-pending") == "succeeded"
    assert len(audits(db, "finish_task", "t-old-pending")) == 1
    assert len(audits(db, "issue_closed", "t-old-pending")) == 1
    landing.landing_tick(POLICY)
    assert len(audits(db, "finish_task", "t-old-pending")) == 1


# --- Outcome evidence: CI at merge, reverts after it ------------------------


def ci_github(monkeypatch, *, statuses=None, runs=None, fail=False):
    """GitHub as ``github`` builds it, plus the two commit CI reads."""
    pulls = {3: pull(3, merged=True)}
    calls = github(
        monkeypatch, pulls=pulls, issues={11: {"number": 11, "state": "closed"}}
    )
    plain = landing.github_get

    def get(repo, suffix):
        if suffix.startswith("commits/"):
            calls["get"].append(suffix)
            if fail:
                raise httpx.ConnectError("down")
            if suffix.endswith("/status"):
                return {"statuses": statuses or []}
            return {"check_runs": runs or []}
        return plain(repo, suffix)

    monkeypatch.setattr(landing, "github_get", get)
    return calls


def test_a_merge_records_the_ci_that_passed_on_its_head(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    ci_github(
        monkeypatch,
        statuses=[{"context": "pr-checks", "state": "success"}],
        runs=[{"name": "lint", "conclusion": "skipped"}],
    )
    landing.landing_tick(POLICY)
    assert audits(db, "merge_ci", "t-1") == [
        {
            "pr_number": 3,
            "head_sha": HEAD,
            "ejections": 0,
            "checks": {"pr-checks": "success", "lint": "skipped"},
            "conclusion": "success",
        }
    ]
    # The merge record itself keeps its shape.
    assert "ci" not in audits(db, "merged", "t-1")[0]


def test_any_failed_check_fails_the_merge_ci(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    ci_github(
        monkeypatch,
        statuses=[{"context": "pr-checks", "state": "success"}],
        runs=[{"name": "flaky", "conclusion": "failure"}],
    )
    landing.landing_tick(POLICY)
    assert audits(db, "merge_ci", "t-1")[0]["conclusion"] == "failure"


def test_an_unreadable_ci_never_blocks_the_merge_record(db, monkeypatch):
    delivered(db, "t-1", 11, 3)
    ci_github(monkeypatch, fail=True)
    landing.landing_tick(POLICY)
    assert audits(db, "merged", "t-1")[0]["pr_number"] == 3
    assert audits(db, "merge_ci", "t-1") == [
        {
            "pr_number": 3,
            "head_sha": HEAD,
            "ejections": 0,
            "conclusion": "unknown",
            "error": "ConnectError",
        }
    ]
    assert audits(db, "issue_closed", "t-1")


def test_ci_conclusion_folds_states():
    assert landing._ci_conclusion([]) == "none"
    assert landing._ci_conclusion(["success", "neutral"]) == "success"
    assert landing._ci_conclusion(["success", "pending"]) == "pending"
    assert landing._ci_conclusion(["pending", "timed_out"]) == "failure"


def merged_audit(db, task_id, pr_number, *, merged_at, sha="c" * 40):
    with Session(db) as session:
        session.add(
            FactoryAudit(
                actor="factory:landing",
                action="merged",
                task_id=task_id,
                created_at=merged_at,
                detail_json=json.dumps(
                    {
                        "pr_number": pr_number,
                        "merge_commit_sha": sha,
                        "armed_by_factory": True,
                    }
                ),
            )
        )
        session.commit()


def history(monkeypatch, commits, *, pages=None):
    """Main's history as GitHub pages it; ``pages`` caps what is returned."""
    from factory.orchestration import factory_reverts as reverts

    calls = []

    def listing(_repo, suffix):
        calls.append(suffix)
        page = int(suffix.rsplit("page=", 1)[1])
        if pages is not None and page > pages:
            raise AssertionError("read past the page budget")
        start = (page - 1) * reverts.PAGE_SIZE
        return commits[start : start + reverts.PAGE_SIZE]

    monkeypatch.setattr(landing, "github_list", listing)
    return calls


def commit(sha, message, date):
    return {
        "sha": sha,
        "commit": {"message": message, "committer": {"date": date}},
    }


def test_a_revert_of_the_merge_commit_is_recorded_once(db, monkeypatch):
    from factory.orchestration import factory_reverts as reverts

    delivered(db, "t-1", 11, 3)
    merged_audit(db, "t-1", 3, merged_at=NOW - timedelta(days=2))
    history(
        monkeypatch,
        [
            commit(
                "d" * 40,
                'Revert "feat: thing"\n\nThis reverts commit ' + "c" * 40 + ".",
                "2026-09-10T09:00:00Z",
            ),
            commit("e" * 40, "chore: unrelated", "2026-09-10T08:00:00Z"),
        ],
    )
    reverts.sweep(POLICY)
    reverts.sweep(POLICY)
    assert audits(db, "reverted", "t-1") == [
        {
            "pr_number": 3,
            "merge_commit_sha": "c" * 40,
            "revert_sha": "d" * 40,
            "revert_committed_at": "2026-09-10T09:00:00Z",
            "detected_at": NOW.isoformat(),
        }
    ]


@pytest.mark.parametrize(
    "message",
    [
        'Revert "feat: thing (#3)"',
        "Revert thing\n\nReverts owner/repo#3",
    ],
)
def test_a_revert_citing_the_pull_request_is_recognised(db, monkeypatch, message):
    from factory.orchestration import factory_reverts as reverts

    delivered(db, "t-1", 11, 3)
    merged_audit(db, "t-1", 3, merged_at=NOW - timedelta(days=1))
    history(monkeypatch, [commit("d" * 40, message, "2026-09-11T00:00:00Z")])
    reverts.sweep(POLICY)
    assert audits(db, "reverted", "t-1")[0]["revert_sha"] == "d" * 40


def test_an_unrelated_pull_request_number_is_not_a_revert(db, monkeypatch):
    from factory.orchestration import factory_reverts as reverts

    delivered(db, "t-1", 11, 3)
    merged_audit(db, "t-1", 3, merged_at=NOW - timedelta(days=1))
    history(
        monkeypatch,
        [commit("d" * 40, "fix: follow up on #3", "2026-09-11T00:00:00Z")],
    )
    reverts.sweep(POLICY)
    assert audits(db, "reverted", "t-1") == []
    # Still inside the window: nothing is closed yet either.
    assert audits(db, "revert_window_closed", "t-1") == []


def test_a_clean_window_closes_once_the_read_is_complete(db, monkeypatch):
    from factory.orchestration import factory_reverts as reverts

    delivered(db, "t-1", 11, 3)
    merged_audit(db, "t-1", 3, merged_at=NOW - timedelta(days=8))
    calls = history(monkeypatch, [commit("e" * 40, "chore: x", "2026-09-05T00:00:00Z")])
    reverts.sweep(POLICY)
    assert audits(db, "revert_window_closed", "t-1") == [
        {
            "pr_number": 3,
            "merge_commit_sha": "c" * 40,
            "window_days": 7,
            "checked_at": NOW.isoformat(),
        }
    ]
    # Judged: the next sweep has nothing to read.
    reverts.sweep(POLICY)
    assert len(calls) == 1


def test_a_truncated_history_never_closes_a_window(db, monkeypatch):
    from factory.orchestration import factory_reverts as reverts

    delivered(db, "t-1", 11, 3)
    merged_audit(db, "t-1", 3, merged_at=NOW - timedelta(days=8))
    full = [
        commit(f"{n:040x}", "chore: x", "2026-09-05T00:00:00Z")
        for n in range(reverts.PAGE_SIZE * reverts.MAX_PAGES)
    ]
    calls = history(monkeypatch, full, pages=reverts.MAX_PAGES)
    reverts.sweep(POLICY)
    assert len(calls) == reverts.MAX_PAGES
    assert audits(db, "revert_window_closed", "t-1") == []


def test_a_revert_after_the_window_does_not_count(db, monkeypatch):
    from factory.orchestration import factory_reverts as reverts

    delivered(db, "t-1", 11, 3)
    merged_audit(db, "t-1", 3, merged_at=NOW - timedelta(days=8, hours=12))
    history(
        monkeypatch,
        [
            commit(
                "d" * 40,
                "Revert x\n\nThis reverts commit " + "c" * 40,
                "2026-09-11T11:00:00Z",
            )
        ],
    )
    reverts.sweep(POLICY)
    assert audits(db, "reverted", "t-1") == []
    assert len(audits(db, "revert_window_closed", "t-1")) == 1


def test_merges_older_than_the_grace_are_never_judged(db, monkeypatch):
    from factory.orchestration import factory_reverts as reverts

    delivered(db, "t-1", 11, 3)
    merged_audit(db, "t-1", 3, merged_at=NOW - timedelta(days=30))
    calls = history(monkeypatch, [])
    reverts.sweep(POLICY)
    assert calls == []
    assert audits(db, "revert_window_closed", "t-1") == []


def test_revert_tick_is_throttled_and_never_raises(monkeypatch):
    from factory.orchestration import factory_reverts as reverts

    ran = []

    def boom(_policy):
        ran.append(1)
        raise RuntimeError("down")

    monkeypatch.setattr(reverts, "sweep", boom)
    monkeypatch.setattr(reverts, "_last_sweep", None)
    reverts.revert_tick(POLICY)
    reverts.revert_tick(POLICY)
    assert ran == [1]


@pytest.mark.parametrize("publication_enabled", ["false", "true"])
@pytest.mark.parametrize("mergeable", [False, True])
def test_dependency_auto_merge_is_disabled_even_with_approved_delivery(
    db, monkeypatch, publication_enabled, mergeable
):
    from factory import review_publisher

    delivered(db, "t-1", 3, 3)
    with Session(db) as session:
        receipt = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == "t-1")
        ).one()
        receipt.direction_json = json.dumps({"dependency_review": {"head_sha": HEAD}})
        session.add(receipt)
        session.commit()
    monkeypatch.setenv("FACTORY_REVIEW_PUBLISH_ENABLED", publication_enabled)
    monkeypatch.setattr(
        review_publisher,
        "collect",
        lambda *_: pytest.fail("publication cannot authorize dependency auto-merge"),
    )
    monkeypatch.setattr(
        landing, "_recover_delivery", lambda *_: pytest.fail("inspection cannot repair")
    )
    pr = pull(3)
    pr["mergeable"] = mergeable
    calls = github(monkeypatch, pulls={3: pr})
    item = landing._deliveries(POLICY)[0]
    landing._arm("owner/repo", item)
    assert calls["graphql"] == []
    assert (
        audits(db, "merge_arm_refused")[0]["reason"] == "dependency_auto_merge_disabled"
    )


def test_dependency_delivery_waits_for_rollout_and_never_closes_pr_as_issue(
    db, monkeypatch
):
    delivered(db, "t-1", 3, 3)
    calls = github(monkeypatch, pulls={3: pull(3, merged=True)})
    item = landing._deliveries(POLICY)[0]
    item["dependency_review"] = {"head_sha": HEAD}
    monkeypatch.setattr(landing, "_verify_rollout", lambda *args: False)
    landing._close_issue("owner/repo", item)
    assert calls["write"] == []
    assert not item["closed"]
    monkeypatch.setattr(landing, "_verify_rollout", lambda *args: True)
    landing._close_issue("owner/repo", item)
    assert calls["write"] == []
    assert item["closed"]
    assert audits(db, "repository_delivery_complete")


@pytest.mark.parametrize(
    "armed,queued", [(False, False), (True, False), (False, True), (True, True)]
)
def test_human_handoff_stops_landing_and_disarms_existing_merge(
    db, monkeypatch, armed, queued
):
    delivered(db, "t-human", 7, 12, state="landing")
    calls = github(
        monkeypatch,
        pulls={
            12: {
                **pull(12, armed=armed),
                "queue_entry": {"id": "MQ_12"} if queued else None,
            }
        },
        issues={7: {"state": "open", "labels": [{"name": "human"}]}},
    )
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-human") == "cancelled"
    assert calls["write"] == []
    assert len(calls["graphql"]) == int(armed) + int(queued)
    assert (
        audits(db, "finish_task", "t-human")[-1]["evidence"]
        == controls.HUMAN_HANDOFF_EVIDENCE
    )


def test_human_handoff_prevents_historical_refusal_from_reopening_delivery(
    db, monkeypatch
):
    delivered(db, "t-human", 7, 12, state="landing")
    landing._record(
        "t-human", "merge_arm_refused", pr_number=12, reason="merge_conflict"
    )
    calls = github(
        monkeypatch,
        pulls={12: pull(12)},
        issues={7: {"state": "open", "labels": [{"name": "human"}]}},
    )
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-human") == "cancelled"
    assert audits(db, "landing_recovery_requested", "t-human") == []
    assert calls["graphql"] == []
    assert calls["write"] == []


def test_human_handoff_waits_for_positive_dequeue_observation(db, monkeypatch):
    delivered(db, "t-human", 7, 12, state="landing")
    github(
        monkeypatch,
        pulls={12: pull(12)},
        issues={7: {"state": "open", "labels": [{"name": "human"}]}},
    )
    monkeypatch.setattr(landing, "_queued_ids", lambda _: {"PR_12"})
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-human") == "landing"
    with Session(db) as session:
        row = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == "t-human")
        ).one()
        assert row.cancellation_requested is True
    assert audits(db, "landing_error", "t-human")[-1]["stage"] == "human_ownership"


@pytest.mark.parametrize(
    "body",
    [
        "Refs #11\n## Live validation outstanding (not claimed)\n- Check archive acknowledgement",
        "Closes #11\n## Live validation outstanding (not claimed)\n- Check archive acknowledgement",
        "Refs #11\nRepository delivery only",
        "Closes #12",
    ],
)
def test_rollout_success_cannot_close_unrecorded_operational_acceptance(
    db, monkeypatch, body
):
    delivered(db, "t-operations", 11, 3)
    pr = {**pull(3, merged=True), "body": body}
    calls = github(monkeypatch, pulls={3: pr}, issues={11: {"state": "open"}})
    item = landing._deliveries(POLICY)[0]
    assert item["conductor_gates"] == []
    landing._close_issue("owner/repo", item)
    assert calls["write"] == []
    assert not audits(db, "issue_closed")
    assert audits(db, "repository_delivery_complete")[0][
        "operational_acceptance_pending"
    ]
    assert (
        item["closed"] is True
    )  # Repository landing is complete, issue acceptance is not.


def test_close_reloads_live_gate_recorded_after_delivery_snapshot(db, monkeypatch):
    delivered(db, "t-late-gate", 11, 3)
    item = landing._deliveries(POLICY)[0]
    with Session(db) as session:
        row = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == "t-late-gate")
        ).one()
        row.direction_json = json.dumps(
            {
                "conductor_gates": [
                    {"kind": "live_validation", "live_checks": ["Verify live flow"]}
                ]
            }
        )
        session.add(row)
        session.commit()
    calls = github(
        monkeypatch, pulls={3: pull(3, merged=True)}, issues={11: {"state": "open"}}
    )
    landing._close_issue("owner/repo", item)
    assert calls["write"] == []
    assert audits(db, "repository_delivery_complete")[0][
        "operational_acceptance_pending"
    ]


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("entry", ["auto-merge", "queue"])
def test_receipt_landing_hold_disarms_and_rechecks_even_with_global_flags_off(
    db, monkeypatch, entry, paused
):
    from factory import review_publisher

    delivered(db, "t-held", 11, 3, settled=NOW - timedelta(days=30))
    with Session(db) as session, session.begin():
        row = controls._receipt(session, "t-held")
        row.direction_json = '{"landing_hold":{"actor":"operator"}}'
        row.task_paused = paused
    pulls = {3: pull(3, armed=entry == "auto-merge")}
    if entry == "queue":
        pulls[3]["queue_entry"] = {"id": "MQ_3", "state": "QUEUED"}
    calls = github(monkeypatch, pulls=pulls)
    monkeypatch.setattr(review_publisher, "enabled", lambda: False)
    off = {**POLICY, "auto_merge": False}
    landing.landing_tick(off)
    assert pulls[3]["auto_merge"] is None
    assert pulls[3].get("queue_entry") is None
    assert (
        audits(db, "merge_arm_refused", "t-held")[0]["reason"] == "receipt_landing_held"
    )
    assert receipt_state(db, "t-held") == "succeeded"
    assert calls["write"] == []
    # A refusal is not a license to forget the hold on later ticks.
    pulls[3]["auto_merge"] = {"merge_method": "REBASE"}
    landing.landing_tick(off)
    assert pulls[3]["auto_merge"] is None
    assert len(audits(db, "merge_arm_refused", "t-held")) == 1
    item = landing._deliveries(POLICY)[0]
    landing._arm(POLICY["repo"], item)
    assert (
        landing._recover_delivery(item, HEAD, "test")["reason"]
        == "receipt_landing_held"
    )
    assert not audits(db, "merge_armed")
    assert not audits(db, "landing_recovery_requested")


@pytest.mark.parametrize("entry", ["auto-merge", "queue"])
def test_typed_unmerged_scope_prevents_landing_with_auto_merge_enabled(
    db, monkeypatch, entry
):
    from factory.orchestration import (
        factory_gates as gates,
        factory_conductor as conductor,
    )

    delivered(db, "t-unmerged", 7040, 7134)
    pulls = {7134: pull(7134, armed=entry == "auto-merge")}
    if entry == "queue":
        pulls[7134]["queue_entry"] = {"id": "MQ_7134", "state": "QUEUED"}
    calls = github(monkeypatch, pulls=pulls)
    monkeypatch.setattr(
        conductor, "github_get", lambda *_: {"body": "Keep this issue open"}
    )
    monkeypatch.setattr(conductor, "_post_decision_card", lambda *_: None)
    gate = {
        "kind": "live_validation",
        "classification": "reversible",
        "reason": "Repository SQL only. Landing requires separate authority.",
        "scope": "Deliver an independently reviewed, unmerged PR.",
        "live_checks": [
            "An authorized operator validates the live schema before deployment."
        ],
        "leave_unmerged": True,
        "effects": {key: False for key in gates.GATE_EFFECTS},
    }
    task = {"id": "t-unmerged", "repo": "owner/repo", "issue_number": 7040}
    assert gates.resolve(task, {"gate": gate}, "unmerged-scope")
    assert task["landing_hold"]
    with Session(db) as session:
        row = controls._receipt(session, "t-unmerged")
        assert controls.landing_held(row)
        assert json.loads(row.direction_json)["conductor_gates"] == [gate]
    calls["write"].clear()
    landing.landing_tick(POLICY)
    assert pulls[7134]["auto_merge"] is None
    assert pulls[7134].get("queue_entry") is None
    assert not audits(db, "merge_armed")
    assert calls["write"] == []
    assert (
        audits(db, "merge_arm_refused", "t-unmerged")[0]["reason"]
        == "receipt_landing_held"
    )


def test_unmerged_hold_is_persisted_before_external_writes(db, monkeypatch):
    from factory.orchestration import (
        factory_gates as gates,
        factory_conductor as conductor,
    )

    delivered(db, "t-unmerged", 7040, 7134)

    def unavailable(*_):
        raise httpx.ConnectError("GitHub unavailable")

    monkeypatch.setattr(conductor, "_post_decision_card", unavailable)
    gate = {
        "kind": "live_validation",
        "classification": "reversible",
        "reason": "No landing authorized",
        "scope": "Unmerged review only",
        "live_checks": ["Separate rollout approval"],
        "leave_unmerged": True,
        "effects": {key: False for key in gates.GATE_EFFECTS},
    }
    with pytest.raises(httpx.ConnectError):
        gates.resolve(
            {"id": "t-unmerged", "repo": "owner/repo", "issue_number": 7040},
            {"gate": gate},
            "unmerged-scope",
        )
    with Session(db) as session:
        assert controls.landing_held(controls._receipt(session, "t-unmerged"))
    with pytest.raises(ValueError, match="invalid gate"):
        gates.validate_gate({**gate, "leave_unmerged": False})


@pytest.mark.parametrize("refused", [False, True])
@pytest.mark.parametrize("merge_enabled", [False, True])
def test_merged_delivery_precedes_publisher_and_recovers_old_refusal(
    db, monkeypatch, refused, merge_enabled
):
    from factory import review_publisher

    delivered(db, "t-1", 11, 3, state="landing")
    if refused:
        landing._record(
            "t-1",
            "merge_arm_refused",
            reason="review_not_published",
            review_reason="pr_not_open",
        )
    monkeypatch.setattr(review_publisher, "enabled", lambda: True)
    monkeypatch.setattr(
        review_publisher,
        "collect",
        lambda *_: pytest.fail("merged PR reached publisher"),
    )
    calls = github(
        monkeypatch, pulls={3: pull(3, merged=True)}, issues={11: {"state": "closed"}}
    )
    for _ in range(3):
        landing.landing_tick({**POLICY, "auto_merge": merge_enabled})
        landing._delivery_cursors.clear()  # Leader restart is a replay, not a new merge.
    assert len(audits(db, "merged", "t-1")) == 1
    assert len(audits(db, "issue_closed", "t-1")) == 1
    assert calls["write"] == calls["graphql"] == []
    assert bool(audits(db, "merge_arm_refused", "t-1")) == refused


@pytest.mark.parametrize("wrong", ["head", "repo", "base", "branch", "merge_sha"])
def test_merged_delivery_requires_exact_owned_identity(db, monkeypatch, wrong):
    delivered(db, "t-1", 11, 3)
    pr = pull(3, merged=True)
    if wrong == "head":
        pr["head"]["sha"] = "c" * 40
    if wrong == "repo":
        pr["head"]["repo"]["full_name"] = "other/repo"
    if wrong == "base":
        pr["base"]["ref"] = "other"
    if wrong == "branch":
        pr["head"]["ref"] = "factory/other"
    if wrong == "merge_sha":
        pr["merge_commit_sha"] = None
    calls = github(monkeypatch, pulls={3: pr})
    landing.landing_tick(POLICY)
    assert audits(db, "merged") == audits(db, "issue_closed") == []
    assert calls["write"] == calls["graphql"] == []


def test_merge_during_publication_recovers_next_tick(db, monkeypatch):
    from factory import review_publisher

    delivered(db, "t-1", 11, 3)
    pr = pull(3)
    calls = github(monkeypatch, pulls={3: pr}, issues={11: {"state": "closed"}})
    monkeypatch.setattr(review_publisher, "enabled", lambda: True)

    def race(_item):
        pr.update(pull(3, merged=True))
        return review_publisher.Refusal("t-1", "pr_not_open")

    monkeypatch.setattr(review_publisher, "collect", race)
    monkeypatch.setattr(review_publisher, "publish", lambda *_: {"action": "refused"})
    landing.landing_tick(POLICY)
    assert audits(db, "merge_arm_refused")
    landing.landing_tick(POLICY)
    assert len(audits(db, "merged")) == 1
    assert calls["graphql"] == []


def test_merge_observation_reaches_beyond_refused_batch(db, monkeypatch):
    monkeypatch.setattr(landing, "OBSERVATION_BATCH", 2)
    for n in range(1, 6):
        delivered(db, f"t-{n}", 10 + n, n)
        landing._record(f"t-{n}", "merge_arm_refused", reason="review_not_published")
    pulls = {n: pull(n, merged=n == 5, task_id=f"t-{n}") for n in range(1, 6)}
    calls = github(monkeypatch, pulls=pulls, issues={15: {"state": "closed"}})
    for _ in range(3):
        landing.landing_tick({"repo": "owner/repo"})
    assert audits(db, "merged", "t-5")
    assert calls["graphql"] == []


def test_paused_merged_delivery_is_not_settled_or_rearmed(db, monkeypatch):
    delivered(db, "t-1", 11, 3, state="landing")
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        row.task_paused = True
        session.commit()
    monkeypatch.setattr(
        landing, "github_get", lambda *_: pytest.fail("paused delivery mutated")
    )
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-1") == "landing"
    assert audits(db, "merged") == []


@pytest.mark.parametrize("blocker", ["reserved", "dispatched", "rollout", "unmerged"])
def test_paused_observed_merge_keeps_remaining_landing_gates(db, monkeypatch, blocker):
    delivered(db, "t-1", 11, 3, state="landing")
    with Session(db) as session, session.begin():
        controls._receipt(session, "t-1").task_paused = True
        ready = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "delivery_ready")
        ).one()
        body = json.loads(ready.detail_json)
        body["evidence"].update(state="merged_reviewed", review_session_id=11)
        ready.detail_json = json.dumps(body)
        if blocker == "reserved":
            session.add(
                FactoryStart(
                    task_id="t-1",
                    start_key="late",
                    actor="test",
                    model="opus",
                    max_cost_usd=1,
                    status="reserved",
                )
            )
        elif blocker == "dispatched":
            session.add(
                SwarmNodeRun(
                    task_id="t-1", node_key="late", attempt=1, status="dispatched"
                )
            )
    landing._record("t-1", "owned_pr_merged", pr_number=3, head_sha=HEAD)
    if blocker == "rollout":
        monkeypatch.setattr(
            factory_rollout,
            "verify",
            lambda *a: {"verified": False, "reason": "test_pending"},
        )
    calls = github(
        monkeypatch,
        pulls={3: pull(3, merged=blocker != "unmerged")},
        issues={11: {"state": "closed"}},
    )
    landing.landing_tick(POLICY)
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-1") == "landing"
    assert not audits(db, "issue_closed")
    assert calls["write"] == calls["graphql"] == []


@pytest.mark.parametrize("new_hold", [None, "pause", "landing"])
@pytest.mark.parametrize(
    "verdict", ["approve", "changes_requested", "wrong_head", "wrong_pr", "ci_pending"]
)
def test_owned_merged_delivery_drains_live_review_before_settlement(
    db, monkeypatch, verdict, new_hold
):
    from factory.orchestration import factory_conductor as conductor, factory_refine
    from factory.orchestration.factory_merged_delivery import reconcile_merged_delivery

    delivered(db, "t-1", 11, 3, state="admitted")
    task = {
        "id": "t-1",
        "repo": "owner/repo",
        "base_branch": "main",
        "conductor_model": "opus",
        "issue_number": 11,
    }
    pr = pull(3, merged=True)
    reads = 0

    def read(_repo, path):
        nonlocal reads
        if path.startswith("pulls/"):
            reads += 1
            if reads == 2 and new_hold:
                with Session(db) as session:
                    row = controls._receipt(session, "t-1")
                    if new_hold == "pause":
                        row.task_paused = True
                    else:
                        row.direction_json = '{"landing_hold":{"actor":"operator"}}'
                    session.commit()
            return pr
        return {
            "statuses": [
                {
                    "context": "pr-checks",
                    "state": "pending" if verdict == "ci_pending" else "success",
                }
            ]
        }

    monkeypatch.setattr(conductor, "github_get", read)
    monkeypatch.setattr(conductor, "pool_for", lambda *_: ["opus"])
    monkeypatch.setattr(factory_refine, "task_class_for", lambda *_: "bug-fix")

    def run(rid, key, session, status, value):
        return {
            "id": rid,
            "node_key": key,
            "session_id": session,
            "status": status,
            "head_sha": HEAD,
            "pin": {"model": "opus"},
            "outcome_json": json.dumps(
                {"value": value, "artifact": {"status": "ok", "value": value}}
            ),
        }

    implementation = run(
        1, "implement_fix", 10, "succeeded", {"pr_number": 3, "head_sha": HEAD}
    )
    review = run(
        2,
        "review_fix",
        11,
        "dispatched",
        {
            "pr_number": 99 if verdict == "wrong_pr" else 3,
            "head_sha": "c" * 40 if verdict == "wrong_head" else HEAD,
            "verdict": "changes_requested"
            if verdict == "changes_requested"
            else "approve",
        },
    )
    with Session(db) as session:
        session.add(
            FactoryStart(
                task_id="t-1",
                start_key="review",
                actor="factory",
                model="opus",
                max_cost_usd=4,
                status="reserved",
            )
        )
        session.commit()
    assert reconcile_merged_delivery(task, POLICY, [implementation, review])
    assert receipt_state(db, "t-1") == "admitted"
    with Session(db) as session:
        start = session.exec(select(FactoryStart)).one()
        assert start.status == "reserved" and start.cost_usd is None
        start.status = "succeeded"
        start.cost_usd = 2
        session.commit()
    review["status"] = "succeeded"
    assert reconcile_merged_delivery(
        task, {**POLICY, "auto_merge": False}, [implementation, review]
    )
    assert receipt_state(db, "t-1") == (
        "admitted"
        if new_hold == "landing" or verdict == "ci_pending"
        else "landing"
        if verdict == "approve"
        else "escalated"
    )
    assert len(audits(db, "owned_pr_merged")) == 1
    assert audits(db, "rollout_verified") == []
    with Session(db) as session:
        assert session.exec(select(FactoryStart.cost_usd)).one() == 2
    if new_hold == "pause" and verdict == "approve":
        calls = github(monkeypatch, pulls={3: pr}, issues={11: {"state": "closed"}})
        landing.landing_tick(POLICY)
        assert receipt_state(db, "t-1") == "succeeded"
        assert len(audits(db, "rollout_verified")) == 1
        assert len(audits(db, "issue_closed")) == 1
        landing.landing_tick(POLICY)
        assert len(audits(db, "issue_closed")) == 1
        assert calls["write"] == calls["graphql"] == []


@pytest.mark.parametrize("hold", ["paused", "landing", "cancelled"])
def test_owned_merge_cannot_clear_receipt_hold(db, monkeypatch, hold):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_merged_delivery import reconcile_merged_delivery

    delivered(db, "t-1", 11, 3, state="admitted")
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        if hold == "paused":
            row.task_paused = True
        if hold == "landing":
            row.direction_json = '{"landing_hold":{"actor":"operator"}}'
        if hold == "cancelled":
            row.cancellation_requested = True
        session.commit()
    monkeypatch.setattr(
        conductor,
        "github_get",
        lambda *_: pytest.fail("held task reached reconciliation"),
    )
    assert not reconcile_merged_delivery({"id": "t-1"}, POLICY, [])
    assert receipt_state(db, "t-1") == "admitted"


@pytest.mark.parametrize("paused", [False, True])
def test_historical_conflict_refusal_cannot_accept_wrong_merged_head(
    db, monkeypatch, paused
):
    delivered(db, "t-1", 11, 3, state="landing")
    landing._record("t-1", "merge_arm_refused", reason="merge_conflict", pr_number=3)
    with Session(db) as session:
        controls._receipt(session, "t-1").task_paused = paused
        session.commit()
    calls = github(monkeypatch, pulls={3: pull(3, merged=True, head="c" * 40)})
    monkeypatch.setattr(
        factory_rollout,
        "verify",
        lambda *_: pytest.fail("wrong-head merge reached rollout"),
    )
    landing.landing_tick(POLICY)
    assert receipt_state(db, "t-1") == "landing"
    assert audits(db, "rollout_verified") == audits(db, "issue_closed") == []
    assert calls["write"] == calls["graphql"] == []
