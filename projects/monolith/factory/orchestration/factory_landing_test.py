"""Landing an approved delivery: arming one merge, then closing its issue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import factory.orchestration.factory_controls as controls
import factory.orchestration.factory_landing as landing
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
    FactoryStart,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
POLICY = {"repo": "owner/repo", "auto_merge": True}
HEAD = "a" * 40


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
                state="succeeded",
                task_class=task_class,
                task_id=task_id,
                updated_at=settled or (NOW - timedelta(minutes=5)),
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


def pull(number, *, merged=False, state="open", draft=False, armed=False, head=HEAD):
    return {
        "number": number,
        "state": "closed" if merged else state,
        "draft": draft,
        "merged": merged,
        "node_id": f"PR_{number}",
        "head": {"sha": head, "ref": f"factory/t-{number}"},
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
        return dict(source[number])

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


def test_landing_is_inert_without_the_flag(db, monkeypatch):
    from factory import review_publisher

    delivered(db, "t-1", 11, 3)
    monkeypatch.setattr(
        review_publisher,
        "collect",
        lambda *_args: pytest.fail("disabled landing collected review evidence"),
    )
    monkeypatch.setattr(
        landing,
        "github_get",
        lambda *_args: pytest.fail("landing read GitHub with auto_merge off"),
    )
    landing.landing_tick({"repo": "owner/repo"})
    landing.landing_tick({"repo": "owner/repo", "auto_merge": False})
    assert audits(db, "merge_armed") == []


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
        lambda *_args: pytest.fail("merge-disabled canary reached arming"),
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
    assert "#3" in body and "observed merge" in body
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
    assert audits(db, "merged", "t-1") == [{"pr_number": 3, "armed_by_factory": False}]
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
    """Model the assessment and fresh independent review before settlement."""
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
                node_key=f"review_recovery_{event.id}",
                attempt=1,
                status="succeeded",
                session_id=12,
                head_sha=head,
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


def test_a_merge_conflict_ejection_reopens_for_correction_instead_of_rearming(
    db, monkeypatch
):
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
    assert [suffix for suffix in calls["get"][reads:]] == ["pulls/4"]


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
        {"stage": "arm", "error": "HTTPStatusError", "status": 403}
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
    assert calls["get"] == ["pulls/3"]
    assert audits(db, "landing_recovery_skipped", "t-1")[0]["reason"] == "head_moved"
    landing.landing_tick(POLICY)
    assert calls["get"] == ["pulls/3", "pulls/4"]
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
