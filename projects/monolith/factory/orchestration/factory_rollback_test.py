"""Offline rollback policy and recovery regressions."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_rollback as rollback
from factory.orchestration.factory_controls import rollback_policy
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
)
from factory.orchestration.models import SwarmTask


def test_default_off():
    assert rollback_policy({}) == {
        "enabled": False,
        "window_minutes": 15,
        "max_per_hour": 2,
        "charts": {},
    }


def test_chart_override():
    policy = rollback_policy(
        {
            "rollback": {
                "charts": {
                    "demo": {"window_minutes": 7, "synthetic_signal": "demo.ready"}
                }
            }
        }
    )
    assert policy["charts"]["demo"] == {
        "window_minutes": 7,
        "synthetic_signal": "demo.ready",
    }
    assert not policy["enabled"]


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        {"enabled": 1},
        {"window_minutes": True},
        {"window_minutes": 0},
        {"max_per_hour": 3},
        {"unknown": False},
        {"charts": {"demo": {"synthetic_signal": ""}}},
        {"charts": {"demo": {"enabled": True}}},
    ],
)
def test_invalid_policy(value):
    with pytest.raises(ValueError):
        rollback_policy({"rollback": value})


NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
POLICY = {"rollback": {"enabled": True}}


def delivery(n=1, **changes):
    return {
        "repo": "owner/repo",
        "task_id": f"task-{n}",
        "pr_number": n,
        "issue_number": n + 10,
        "merge_commit_sha": f"{n:040x}",
        "application": "demo",
        "chart": "demo",
        "revision": "1.2.3",
        "merged_at": (NOW - timedelta(minutes=1)).isoformat(),
        **changes,
    }


def sample(d=None, **changes):
    d = d or delivery()
    return {
        **{
            k: d[k]
            for k in (
                "repo",
                "task_id",
                "merge_commit_sha",
                "application",
                "chart",
                "revision",
            )
        },
        "signal": "argocd",
        "outcome": "failed",
        "available": True,
        "observed_at": NOW.isoformat(),
        **changes,
    }


@pytest.fixture
def engine(tmp_path, monkeypatch):
    result = create_engine(
        f"sqlite:///{tmp_path / 'rollback.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={"schema_translate_map": {"swarm": None}},
    )
    SQLModel.metadata.create_all(
        result,
        tables=[
            m.__table__
            for m in (SwarmTask, FactoryControl, FactoryReceipt, FactoryAudit)
        ],
    )
    with Session(result) as db:
        db.add(
            FactoryControl(
                id="factory",
                state="enabled",
                actor="test",
                policy_json=json.dumps(POLICY),
            )
        )
        db.commit()
    monkeypatch.setattr(controls, "get_engine", lambda: result)
    yield result
    result.dispose()


def seed(engine, d):
    with Session(engine) as db:
        db.add(
            SwarmTask(
                id=d["task_id"],
                task_text="test",
                repo=d["repo"],
                conductor_model="opus",
            )
        )
        db.add(
            FactoryReceipt(
                repo=d["repo"],
                issue_number=d["issue_number"],
                generation=0,
                title="test",
                body="",
                url="",
                actor="test",
                task_id=d["task_id"],
                state="landing",
            )
        )
        db.add(
            FactoryAudit(
                actor="test",
                task_id=d["task_id"],
                action="merged",
                detail_json=json.dumps(
                    {
                        "pr_number": d["pr_number"],
                        "merge_commit_sha": d["merge_commit_sha"],
                    }
                ),
            )
        )
        db.commit()


def events(engine, action):
    with Session(engine) as db:
        return [
            json.loads(row.detail_json)
            for row in db.exec(
                select(FactoryAudit).where(FactoryAudit.action == action)
            ).all()
        ]


@pytest.mark.parametrize("signal", ["argocd", "kargo_promotion", "revision_health"])
def test_pipeline_failure_contract(signal):
    result = rollback.verdict(POLICY, delivery(), [sample(signal=signal)], now=NOW)
    assert result["verdict"] == "rollout_failed"
    assert result["failure"]["signal"] == signal


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": "1.2.2"},
        {"application": "other"},
        {"chart": "other"},
        {"repo": "other/repo"},
        {"task_id": "other"},
        {"merge_commit_sha": "f" * 40},
        {"available": False},
        {"available": 1},
        {"observed_at": None},
        {"observed_at": "bad"},
        {"observed_at": (NOW - timedelta(minutes=2)).isoformat()},
        {"observed_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"signal": "unnamed"},
        {"outcome": "degraded?"},
    ],
)
@pytest.mark.parametrize("outcome", ["failed", "healthy"])
def test_unrelated_or_unknown_cannot_fail_or_verify(changes, outcome):
    assert (
        rollback.verdict(
            POLICY, delivery(), [sample(**{"outcome": outcome, **changes})], now=NOW
        )["verdict"]
        == "unknown"
    )


def test_synthetic_identity_is_explicit():
    policy = {"rollback": {"charts": {"demo": {"synthetic_signal": "smoke"}}}}
    assert (
        rollback.verdict(
            policy, delivery(), [sample(signal="synthetic:smoke")], now=NOW
        )["verdict"]
        == "rollout_failed"
    )
    assert (
        rollback.verdict(
            policy, delivery(), [sample(signal="synthetic:other")], now=NOW
        )["verdict"]
        == "unknown"
    )
    assert (
        rollback.verdict({}, delivery(), [sample(signal="synthetic:smoke")], now=NOW)[
            "verdict"
        ]
        == "unknown"
    )


def test_healthy_window_and_per_chart_override():
    d = delivery(
        merged_at=NOW - timedelta(minutes=20),
        window_started_at=NOW - timedelta(minutes=7),
    )
    healthy = [sample(d, outcome="healthy")]
    assert (
        rollback.verdict(POLICY, d, healthy, now=NOW)["verdict"] == "rollout_observing"
    )
    assert (
        rollback.verdict(
            {"rollback": {"charts": {"demo": {"window_minutes": 7}}}},
            d,
            healthy,
            now=NOW,
        )["verdict"]
        == "rollout_verified"
    )
    assert (
        rollback.verdict(
            POLICY,
            {**d, "window_started_at": NOW - timedelta(minutes=15)},
            healthy,
            now=NOW,
        )["verdict"]
        == "rollout_verified"
    )
    assert rollback.verdict(POLICY, d, [], now=NOW)["verdict"] == "unknown"
    assert (
        rollback.verdict(POLICY, {**d, "window_started_at": None}, healthy, now=NOW)[
            "verdict"
        ]
        == "rollout_observing"
    )


def test_post_window_failure_is_not_delivery_failure():
    d = delivery(
        merged_at=NOW - timedelta(minutes=30),
        window_started_at=NOW - timedelta(minutes=16),
    )
    assert rollback.verdict(POLICY, d, [sample(d)], now=NOW)["verdict"] == "unknown"


def test_concurrent_reservation_and_effect_are_once_only(engine):
    d = delivery()
    seed(engine, d)
    barrier = Barrier(2)

    def run():
        barrier.wait()
        return rollback.reserve(POLICY, d, sample(d), now=NOW)["state"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        assert sorted(f.result() for f in futures) == ["existing", "reserved"]
    assert len(events(engine, "rollback_reserved")) == 1
    # Fresh sessions simulate restart. Neither replay nor another application
    # from the same delivery can reserve a second automatic rollback.
    assert (
        rollback.reserve(
            POLICY, {**d, "application": "demo-other"}, sample(d), now=NOW
        )["state"]
        == "existing"
    )

    def begin():
        barrier.wait()
        return rollback.begin_pr_creation(d, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(begin) for _ in range(2)]
        assert sorted(f.result() for f in futures) == [False, True]
    assert not rollback.begin_pr_creation(d, now=NOW)
    assert len(events(engine, "rollback_pr_started")) == 1


def pull(d, **changes):
    key = rollback.identity(d)
    return {
        "number": 100,
        "head": {
            "ref": "factory/rollback-" + key[:32],
            "sha": "a" * 40,
            "repo": {"full_name": d["repo"]},
        },
        "body": f"Reverts #{d['pr_number']}",
        "title": "revert(demo): recover rollout",
        "merged": False,
        "state": "open",
        **changes,
    }


def test_lost_pr_response_reconciles_and_lands_once(engine):
    d = delivery()
    seed(engine, d)
    rollback.reserve(POLICY, d, sample(d), now=NOW)
    assert rollback.begin_pr_creation(d, now=NOW)
    # An external create succeeded but its response was lost. A fresh process
    # reads the deterministic branch and records the same PR, never reissues.
    assert (
        rollback.observe_pr(d, pull(d, auto_merge={"enabled": True}), now=NOW)["state"]
        == "review_and_queue_pending"
    )
    assert not events(engine, "rollback_landed")
    assert (
        rollback.observe_pr(d, pull(d, state="closed"), now=NOW)["state"]
        == "review_and_queue_pending"
    )
    landed = pull(d, merged=True, merge_commit_sha="b" * 40, merged_at=NOW.isoformat())
    assert rollback.observe_pr(d, landed, now=NOW)["state"] == "rollback_landed"
    assert rollback.observe_pr(d, landed, now=NOW)["state"] == "rollback_landed"
    assert len(events(engine, "rollback_pr_observed")) == 1
    assert len(events(engine, "rollback_landed")) == 1
    assert events(engine, "rollback_landed")[0]["elapsed_seconds"] == 0
    assert not events(engine, "reverted")  # Settlement belongs to the landing owner.


def test_disable_stops_new_intents_but_observation_continues(engine):
    d = delivery()
    seed(engine, d)
    rollback.reserve(POLICY, d, sample(d), now=NOW)
    rollback.begin_pr_creation(d, now=NOW)
    with controls._locked_session() as (_db, control):
        control.policy_json = json.dumps({"rollback": {"enabled": False}})
    other = delivery(2)
    seed(engine, other)
    assert (
        rollback.reserve(POLICY, other, sample(other), now=NOW)["state"] == "disabled"
    )
    assert rollback.reserve({}, d, sample(d), now=NOW)["state"] == "existing"
    assert (
        rollback.observe_pr(d, pull(d), now=NOW)["state"] == "review_and_queue_pending"
    )


def test_breaker_two_then_third_and_replay(engine):
    for n in range(1, 4):
        seed(engine, delivery(n))
    for n in (1, 2):
        d = delivery(n)
        assert rollback.reserve(POLICY, d, sample(d), now=NOW)["state"] == "reserved"
    d = delivery(3)
    for _ in range(3):
        assert (
            rollback.reserve(POLICY, d, sample(d), now=NOW)["state"] == "breaker_open"
        )
    assert len(events(engine, "rollback_breaker_opened")) == 1
    assert len(events(engine, "rollback_reserved")) == 2
    later = NOW + timedelta(hours=1)
    assert (
        rollback.reserve(
            POLICY, d, sample(d, observed_at=later.isoformat()), now=later
        )["state"]
        == "reserved"
    )


def test_revert_of_revert_and_holds(engine):
    d = delivery(is_revert=True)
    seed(engine, d)
    assert (
        rollback.reserve(POLICY, d, sample(d), now=NOW)["reason"]
        == "automatic_revert_of_revert"
    )
    with controls._locked_session() as (db, _control):
        controls._receipt(db, d["task_id"]).task_paused = True
    assert rollback.reserve(POLICY, d, sample(d), now=NOW)["state"] == "held"
    assert not events(engine, "rollback_reserved")


@pytest.mark.parametrize(
    "changes",
    [
        {"body": None},
        {"title": "wrong"},
        {"number": True},
        {"head": {"ref": "factory/other"}},
        {"merge_commit_sha": "bad", "merged": True},
        {
            "merged_at": (NOW + timedelta(minutes=1)).isoformat(),
            "merge_commit_sha": "b" * 40,
            "merged": True,
        },
    ],
)
def test_pr_identity_and_time_cannot_fabricate_landing(engine, changes):
    d = delivery()
    seed(engine, d)
    rollback.reserve(POLICY, d, sample(d), now=NOW)
    rollback.begin_pr_creation(d, now=NOW)
    assert rollback.observe_pr(d, pull(d, **changes), now=NOW)["state"] in {
        "identity_unknown",
        "merge_unknown",
    }
    assert not events(engine, "rollback_landed")


def test_sqlite_naive_and_postgres_aware_timestamps_agree():
    assert rollback.utc(NOW.replace(tzinfo=None)) == rollback.utc(NOW) == NOW


@pytest.mark.parametrize("merge_commit", [False, True])
def test_reverse_source_uses_real_git_revert_and_resumes(tmp_path, merge_commit):
    path = tmp_path / "repo"
    path.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
            env={
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "Rollback test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Rollback test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            },
        ).stdout.strip()

    git("init", "-b", "main")
    (path / "service").write_text("healthy\n")
    git("add", "service")
    git("commit", "-m", "test: healthy base")
    if merge_commit:
        git("checkout", "-b", "feature")
    (path / "service").write_text("broken\n")
    git("add", "service")
    git("commit", "-m", "test: changed service")
    if merge_commit:
        git("checkout", "main")
        git("merge", "--no-ff", "feature", "-m", "test: land service")
    d = delivery(merge_commit_sha=git("rev-parse", "HEAD"))
    branch = "factory/rollback-" + rollback.identity(d)[:32]
    git("checkout", "-b", branch)
    # Supply an isolated test identity to the production subprocess seam.
    import os
    from unittest.mock import patch

    with patch.dict(
        os.environ,
        {
            "GIT_AUTHOR_NAME": "Rollback test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Rollback test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    ):
        result = rollback.reverse_source(str(path), d)
        assert (path / "service").read_text() == "healthy\n"
        assert rollback.reverse_source(str(path), d) == result
    assert f"This reverts commit {d['merge_commit_sha']}" in git(
        "log", "-1", "--format=%B"
    )
    assert result["head_sha"] != d["merge_commit_sha"]
