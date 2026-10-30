"""Offline rollback policy and recovery regressions."""

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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
    FactoryStart,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask


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


NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
POLICY = {"rollback": {"enabled": True}}


def delivery(n=1, **changes):
    return {
        "repo": "owner/repo",
        "task_id": f"task-{n}",
        "pr_number": n,
        "issue_number": n + 10,
        "merge_commit_sha": f"{n:040x}",
        "commit_count": 1,
        "source_checked": True,
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
            for m in (
                SwarmTask,
                SwarmNodeRun,
                FactoryControl,
                FactoryReceipt,
                FactoryAudit,
                FactoryStart,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
            )
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
        "base": {"ref": "main"},
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
    assert rollback.reserve(POLICY, d, sample(d), now=NOW)["state"] == "needs_human"
    d = {**d, "is_revert": False}
    assert rollback.reserve(POLICY, d, sample(d), now=NOW)["state"] == "held"
    assert not events(engine, "rollback_reserved")


@pytest.mark.parametrize(
    "changes",
    [
        {"body": None},
        {"base": {"ref": "other"}},
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


@pytest.mark.parametrize("merge_commit,count", [(False, 1), (False, 2), (True, 2)])
def test_reverse_source_uses_real_git_revert_and_resumes(tmp_path, merge_commit, count):
    path = tmp_path / "repo"
    path.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
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
    if merge_commit or count == 2:
        git("checkout", "-b", "feature")
    (path / "service").write_text("broken\n")
    git("add", "service")
    git("commit", "-m", "test: changed service")
    source_commits = [git("rev-parse", "HEAD")]
    if count == 2:
        (path / "service").write_text("broken twice\n")
        git("add", "service")
        git("commit", "-m", "test: second change")
        source_commits.append(git("rev-parse", "HEAD"))
    if merge_commit:
        git("checkout", "main")
        git("merge", "--no-ff", "feature", "-m", "test: land service")
    elif count == 2:
        git("checkout", "main")
        (path / "unrelated").write_text("another delivery\n")
        git("add", "unrelated")
        git("commit", "-m", "test: unrelated main change")
        git("cherry-pick", *source_commits)
    d = delivery(merge_commit_sha=git("rev-parse", "HEAD"), commit_count=count)
    branch = "factory/rollback-" + rollback.identity(d)[:32]
    git("checkout", "-b", branch)
    from factory.orchestration.factory_rollback_source import validate_landed_range

    validate_landed_range(str(path), d, source_commits)
    with pytest.raises(ValueError, match="inventory incomplete"):
        validate_landed_range(str(path), d, [])
    # Supply an isolated test identity to the production subprocess seam.
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
    if count == 2 and not merge_commit:
        assert (path / "unrelated").read_text() == "another delivery\n"


def test_changed_head_can_be_observed_then_landed(engine):
    d = delivery()
    seed(engine, d)
    rollback.reserve(POLICY, d, sample(d), now=NOW)
    rollback.begin_pr_creation(d, now=NOW)
    p = pull(d)
    rollback.observe_pr(d, p, now=NOW)
    p["head"]["sha"] = "c" * 40
    assert rollback.observe_pr(d, p, now=NOW)["state"] == "head_moved"
    assert events(engine, "rollback_pr_observed")[-1]["review_invalidated"]
    p.update(merged=True, merge_commit_sha="d" * 40, merged_at=NOW.isoformat())
    assert rollback.observe_pr(d, p, now=NOW)["state"] == "rollback_landed"


def test_failure_is_durable_even_disabled_and_never_verifies(engine):
    d = delivery()
    seed(engine, d)
    with controls._locked_session() as (_db, control):
        control.policy_json = json.dumps({"rollback": {"enabled": False}})
    assert rollback.reserve({}, d, sample(d), now=NOW)["state"] == "disabled"
    later = NOW + timedelta(minutes=16)
    for _ in range(2):
        result = rollback.observe_window(
            {},
            d,
            [sample(d, outcome="healthy", observed_at=later.isoformat())],
            now=later,
        )
        assert result["verdict"] == "rollout_failed"
    assert len(events(engine, "rollout_failed")) == 1
    assert not events(engine, "rollback_window_verified")


def test_window_survives_restart(engine):
    d = delivery()
    seed(engine, d)
    for minute, expected in [
        (0, "rollout_observing"),
        (14, "rollout_observing"),
        (15, "rollout_verified"),
    ]:
        now = NOW + timedelta(minutes=minute)
        assert (
            rollback.observe_window(
                {},
                d,
                [sample(d, outcome="healthy", observed_at=now.isoformat())],
                now=now,
            )["verdict"]
            == expected
        )
    assert len(events(engine, "rollback_window_started")) == 1
    assert len(events(engine, "rollback_window_verified")) == 1


def test_breaker_is_factory_wide(engine):
    for n in range(1, 5):
        d = delivery(n, repo=f"owner/repo-{n}")
        seed(engine, d)
        assert rollback.reserve(POLICY, d, sample(d), now=NOW)["state"] == (
            "reserved" if n <= 2 else "breaker_open"
        )
    assert len(events(engine, "rollback_breaker_opened")) == 1


@pytest.mark.parametrize(
    "source",
    [
        {"title": 'Revert "fix"'},
        {"title": "revert(demo): fix"},
        {"body": "Reverts #12"},
        {"commit_message": "This reverts commit " + "b" * 40},
    ],
)
def test_source_revert_detected_without_receipt_or_observation(engine, source):
    d = delivery(**source)
    assert rollback.reserve(POLICY, d, sample(d), now=NOW)["state"] == "needs_human"


@pytest.fixture
def runtime(engine, monkeypatch):
    from factory.orchestration import factory_landing as landing
    from factory.orchestration import factory_rollback_runtime as runtime

    monkeypatch.setattr(landing, "_now", lambda: NOW)
    monkeypatch.setattr(controls, "_now", lambda: NOW)
    # Every external read/write is a local fake. Unexpected paths fail tests.
    issues = {
        11: {
            "number": 11,
            "title": "original",
            "body": "original body",
            "labels": [],
            "state": "closed",
        }
    }
    writes = []
    pulls = []

    def get(_repo, path):
        if path.startswith("issues/"):
            return json.loads(json.dumps(issues[int(path.split("/")[1])]))
        if path.startswith("pulls/"):
            return next(p for p in pulls if p["number"] == int(path.split("/")[1]))
        raise AssertionError(path)

    def listing(_repo, path):
        if path.startswith("issues?"):
            return list(issues.values())
        if path.startswith("pulls?"):
            return pulls
        raise AssertionError(path)

    def write(_repo, path, body, *, method="POST"):
        writes.append((path, body, method))
        if path == "issues":
            number = max(issues) + 1
            value = {
                "number": number,
                "state": "open",
                **body,
                "labels": [{"name": n} for n in body.get("labels", [])],
            }
            issues[number] = value
            return value
        number = int(path.split("/")[1])
        if path.endswith("/labels"):
            issues[number]["labels"].extend({"name": name} for name in body["labels"])
        elif method == "PATCH":
            issues[number].update(body)
        else:
            raise AssertionError(path)
        return issues[number]

    monkeypatch.setattr(landing, "github_get", get)
    monkeypatch.setattr(landing, "github_list", listing)
    monkeypatch.setattr(landing, "github_write", write)
    monkeypatch.setattr(
        landing, "_error", lambda _task, _where, exc: (_ for _ in ()).throw(exc)
    )
    return runtime, issues, writes, pulls


def test_issue_create_crash_reconciles_marker_once(engine, runtime, monkeypatch):
    r, issues, writes, _pulls = runtime
    d = delivery()
    seed(engine, d)
    original = r._record

    def crash(delivery, action, token, **detail):
        if action == "rollback_external_issue_observed":
            raise RuntimeError("crash after issue creation")
        return original(delivery, action, token, **detail)

    monkeypatch.setattr(r, "_record", crash)
    with pytest.raises(RuntimeError):
        r._issue_once(d, "breaker:one", "breaker", "evidence", ["needs-human"])
    monkeypatch.setattr(r, "_record", original)
    for _ in range(2):
        assert (
            r._issue_once(d, "breaker:one", "breaker", "evidence", ["needs-human"])[
                "number"
            ]
            == 12
        )
    assert len([w for w in writes if w[0] == "issues"]) == 1
    assert issues[12]["labels"] == [{"name": "needs-human"}]


def test_issue_create_fence_unknown_never_duplicates(engine, runtime):
    r, _issues, writes, _pulls = runtime
    d = delivery()
    seed(engine, d)
    r._record(d, "rollback_external_issue_started", "unknown")
    assert r._issue_once(d, "unknown", "breaker", "evidence", []) is None
    assert not writes


def test_dispatch_replays_same_normal_receipt_after_fence(engine, runtime):
    r, _issues, writes, _pulls = runtime
    d = delivery()
    seed(engine, d)
    rollback.reserve(POLICY, d, sample(d), now=NOW)
    # A fence committed with no create is reconciled by the deterministic
    # recovery receipt. It does not strand the delivery after a process crash.
    rollback.begin_pr_creation(d, now=NOW)
    for _ in range(2):
        r._dispatch(d, {"generation": 0})
    assert len([w for w in writes if w[0] == "issues"]) == 1
    receipts = events(engine, "rollback_receipt")
    assert len(receipts) == 1
    with Session(engine) as db:
        row = db.get(FactoryReceipt, receipts[0]["receipt_id"])
        assert row.state == "queued"  # Ordinary admission still owns starts.
        assert row.actor == controls.INTAKE_ACTOR
        assert r.admission_identity(db, row) == "rollback-" + rollback.identity(d)[:32]
        controls._receipt(db, d["task_id"]).task_paused = True
        db.commit()
        assert r.parent_held(db, row)
        with pytest.raises(ValueError, match="held"):
            r.admission_identity(db, row)


@pytest.mark.parametrize("label", ["human", "needs-human"])
def test_human_ownership_blocks_dispatch(engine, runtime, label):
    r, issues, writes, _pulls = runtime
    d = delivery()
    seed(engine, d)
    rollback.reserve(POLICY, d, sample(d), now=NOW)
    issues[11]["labels"] = [{"name": label}]
    r._dispatch(d, {"generation": 0})
    assert not writes


def test_reopen_crash_then_settlement_only_after_actual_merge(
    engine, runtime, monkeypatch
):
    r, issues, writes, _pulls = runtime
    d = delivery()
    seed(engine, d)
    rollback.reserve(POLICY, d, sample(d), now=NOW)
    rollback.begin_pr_creation(d, now=NOW)
    p = pull(d)
    rollback.observe_pr(d, p, now=NOW)
    assert not r.settle(d)
    p.update(merged=True, merge_commit_sha="e" * 40, merged_at=NOW.isoformat())
    rollback.observe_pr(d, p, now=NOW)
    original = r._record

    def crash(delivery, action, token, **detail):
        if action == "rollback_issue_reopened":
            raise RuntimeError("crash after reopen")
        return original(delivery, action, token, **detail)

    monkeypatch.setattr(r, "_record", crash)
    with pytest.raises(RuntimeError):
        r._reopen(d, p)
    assert not r.settle(d)
    monkeypatch.setattr(r, "_record", original)
    r._reopen(d, p)
    r.settle(d)
    assert issues[11]["state"] == "open"
    assert issues[11]["labels"] == [{"name": "agent-ready"}]
    assert len([w for w in writes if w[2] == "PATCH"]) == 1
    assert len(events(engine, "reverted")) == 1
    assert len(events(engine, "rollback_settled")) == 1
    with Session(engine) as db:
        assert controls._receipt(db, d["task_id"]).state == "failed"


def test_reopen_preserves_human_ownership_and_hold(engine, runtime):
    r, issues, _writes, _pulls = runtime
    d = delivery()
    seed(engine, d)
    rollback.reserve(POLICY, d, sample(d), now=NOW)
    rollback.begin_pr_creation(d, now=NOW)
    p = pull(d, merged=True, merge_commit_sha="e" * 40, merged_at=NOW.isoformat())
    rollback.observe_pr(d, p, now=NOW)
    issues[11]["labels"] = [{"name": "human"}]
    with controls._locked_session() as (db, _control):
        controls._receipt(db, d["task_id"]).direction_json = json.dumps(
            {"landing_hold": {"reason": "hold"}}
        )
    r._reopen(d, p)
    assert issues[11]["labels"] == [{"name": "human"}]
    with Session(engine) as db:
        assert controls.landing_held(controls._receipt(db, d["task_id"]))


def test_landing_caller_observes_healthy_window_without_intent(
    engine, runtime, monkeypatch
):
    r, _issues, _writes, _pulls = runtime
    d = delivery()
    seed(engine, d)
    p = {
        "merged": True,
        "merged_at": d["merged_at"],
        "merge_commit_sha": d["merge_commit_sha"],
        "commits": 1,
    }
    for minute, expected in [(0, "rollout_observing"), (15, "rollout_verified")]:
        now = NOW + timedelta(minutes=minute)
        monkeypatch.setattr(r.landing, "_now", lambda now=now: now)
        target = {k: d[k] for k in ("application", "chart", "revision")}
        target["observations"] = [
            sample(d, outcome="healthy", observed_at=now.isoformat())
        ]
        assert r.observe_targets({"repo": d["repo"]}, d, p, [target]) == expected
    assert not events(engine, "rollback_reserved")


def test_disabled_runtime_observes_head_move_and_merge(engine, runtime):
    r, issues, _writes, pulls = runtime
    d = delivery()
    seed(engine, d)
    rollback.reserve(POLICY, d, sample(d), now=NOW)
    rollback.begin_pr_creation(d, now=NOW)
    with controls._locked_session() as (_db, control):
        control.policy_json = json.dumps({"rollback": {"enabled": False}})
    p = pull(d)
    pulls.append(p)
    r.tick({"repo": d["repo"]})
    p["head"]["sha"] = "f" * 40
    r.tick({"repo": d["repo"]})
    assert not events(engine, "rollback_landed")
    p.update(merged=True, merged_at=NOW.isoformat(), merge_commit_sha="e" * 40)
    r.tick({"repo": d["repo"]})
    assert not events(engine, "rollback_landed")  # Ordinary owner has not attested.
    child = delivery(2, task_id="rollback-" + rollback.identity(d)[:32])
    seed(engine, child)
    with controls._locked_session() as (db, _control):
        child_row = controls._receipt(db, child["task_id"])
        rollback._append(
            db,
            d,
            "rollback_receipt",
            rollback.identity(d),
            NOW,
            receipt_id=child_row.id,
        )
        controls._audit(
            db,
            r.landing.ACTOR,
            "merged",
            task_id=child["task_id"],
            pr_number=p["number"],
            merge_commit_sha=p["merge_commit_sha"],
        )
        controls._audit(
            db,
            "factory:reconciler",
            "delivery_ready",
            task_id=child["task_id"],
            evidence={
                "pr_url": f"https://github.com/{d['repo']}/pull/{p['number']}",
                "head_sha": p["head"]["sha"],
            },
        )
    r.tick({"repo": d["repo"]})
    assert len(events(engine, "rollback_landed")) == 1
    assert issues[11]["state"] == "open"


def test_generated_revert_is_recognized_by_outcome_sweep():
    from factory.orchestration.factory_reverts import revert_evidence

    assert revert_evidence("revert(demo): recover\n\nReverts #12")[1] == {12}


@pytest.mark.parametrize("failed", [False, True])
def test_actual_landing_window_caller_keeps_failure_terminal(
    engine, runtime, monkeypatch, failed
):
    from factory.orchestration import factory_rollout

    r, _issues, _writes, pulls = runtime
    d = delivery()
    seed(engine, d)
    pulls.append(
        {
            "number": 1,
            "merged": True,
            "merged_at": d["merged_at"],
            "merge_commit_sha": d["merge_commit_sha"],
            "commits": 1,
        }
    )
    target = {k: d[k] for k in ("application", "chart", "revision")}
    target["observations"] = [sample(d, outcome="failed" if failed else "healthy")]
    monkeypatch.setattr(
        factory_rollout,
        "verify",
        lambda *_a: {"verified": not failed, "rollback_targets": [target]},
    )
    monkeypatch.setattr(
        factory_rollout, "collect_rollback_signals", lambda _p, targets: targets
    )
    item = {**d, "approved_head_sha": "a" * 40}
    assert not r.landing._verify_rollout(d["repo"], item)
    later = NOW + timedelta(minutes=15)
    monkeypatch.setattr(r.landing, "_now", lambda: later)
    target["observations"] = [
        sample(d, outcome="healthy", observed_at=later.isoformat())
    ]
    # A later read that loses its target scope cannot erase an earlier failure.
    monkeypatch.setattr(
        factory_rollout,
        "verify",
        lambda *_a: {
            "verified": True,
            **({} if failed else {"rollback_targets": [target]}),
        },
    )
    assert r.landing._verify_rollout(d["repo"], item) is (not failed)
    assert len(events(engine, "rollout_failed")) == int(failed)
    assert len(events(engine, "rollout_verified")) == int(not failed)
    assert not events(engine, "rollback_reserved")


@pytest.mark.parametrize(
    "message,is_revert",
    [("feat: normal", False), ("This reverts commit " + "b" * 40, True)],
)
def test_source_details_reads_commit_evidence_before_reservation(
    engine, runtime, monkeypatch, message, is_revert
):
    r, _issues, _writes, pulls = runtime
    d = delivery(source_checked=False)
    pulls.append(
        {
            "number": 1,
            "merged": True,
            "merge_commit_sha": d["merge_commit_sha"],
            "commits": 2,
        }
    )
    monkeypatch.setattr(
        r.landing,
        "github_list",
        lambda _r, _p: [
            {"commit": {"message": "feat: first"}},
            {"commit": {"message": message}},
        ],
    )
    enriched = r.source_details(d)
    assert enriched["source_checked"] is True
    assert enriched["is_revert"] is is_revert
    assert enriched["commit_count"] == 2
    if is_revert:
        assert (
            rollback.reserve(POLICY, enriched, sample(d), now=NOW)["state"]
            == "needs_human"
        )
    pulls[0]["commits"] = 3
    with pytest.raises(ValueError, match="inventory incomplete"):
        r.source_details(d)


def test_runtime_breaker_issue_created_once(engine, runtime, monkeypatch):
    r, _issues, writes, _pulls = runtime
    for n in range(1, 4):
        d = delivery(n)
        seed(engine, d)
        rollback.reserve(POLICY, d, sample(d), now=NOW)
    monkeypatch.setattr(r, "_dispatch", lambda *_a: None)
    # No new reservations while disabled, but the already-opened breaker is reconciled.
    for _ in range(2):
        r.tick({"repo": "owner/repo"})
    created = [w for w in writes if w[0] == "issues"]
    assert len(created) == 1
    assert created[0][1]["labels"] == ["needs-human"]


@pytest.mark.parametrize("lost_response", [False, True])
def test_source_worker_replays_same_pr_after_create(engine, monkeypatch, lost_response):
    from factory.orchestration import factory_rollback_source as worker

    d = delivery()
    branch = "factory/rollback-" + rollback.identity(d)[:32]
    result = {"branch": branch, "head_sha": "d" * 40}
    created, calls = [], []

    def run(_checkout, *args):
        calls.append(args)
        if args[:3] == ("git", "rev-parse", "FETCH_HEAD"):
            return "b" * 40
        if args[0] == "git":
            assert args[1] in {"fetch", "push"}
            return ""
        if args[:3] == ("gh", "pr", "create"):
            created.append({"number": 99})
            if lost_response:
                raise subprocess.CalledProcessError(1, args)
            return "https://github.com/owner/repo/pull/99"
        assert args[:2] == ("gh", "api")
        path = args[2]
        if path.endswith("/pulls/1"):
            return json.dumps(
                {
                    "merged": True,
                    "merge_commit_sha": d["merge_commit_sha"],
                    "commits": 1,
                    "head": {"sha": "b" * 40},
                }
            )
        if "/commits?" in path:
            return json.dumps([{"sha": "b" * 40}])
        if "pulls?state=all" in path:
            return json.dumps(created)
        assert path.endswith("/pulls/99")
        return json.dumps({"number": 99, "head": {"sha": result["head_sha"]}})

    monkeypatch.setattr(worker, "_run", run)
    monkeypatch.setattr(worker, "reverse_source", lambda *_a: result)
    monkeypatch.setattr(worker, "validate_landed_range", lambda *_a: None)
    for _ in range(2):
        assert worker.publish("unused", d, 12)["pr_number"] == 99
    assert len(created) == 1
    assert len([c for c in calls if c[:3] == ("gh", "pr", "create")]) == 1


def test_named_synthetic_requires_exact_revision(engine):
    from core.platform_probe import PlatformProbe

    from factory.orchestration.factory_rollout import _rollback_synthetic

    PlatformProbe.__table__.create(engine)
    d = delivery()
    detail = {k: d[k] for k in ("application", "chart", "revision")}
    with Session(engine) as db:
        db.add(
            PlatformProbe(
                name="demo.ready", ok=False, checked_at=NOW, detail=json.dumps(detail)
            )
        )
        db.commit()
    result = _rollback_synthetic("demo.ready", d)
    assert result["outcome"] == "failed"
    assert result["signal"] == "synthetic:demo.ready"
    assert _rollback_synthetic("other", d) is None
    assert _rollback_synthetic("demo.ready", {**d, "revision": "1.2.4"}) is None


def test_landing_caller_enforces_source_hold_on_recovery(engine, runtime, monkeypatch):
    r, _issues, _writes, pulls = runtime
    source = delivery()
    child = delivery(2)
    seed(engine, source)
    seed(engine, child)
    with controls._locked_session() as (db, _control):
        controls._receipt(db, source["task_id"]).task_paused = True
        controls._receipt(db, child["task_id"]).direction_json = json.dumps(
            {"rollback_parent_task_id": source["task_id"]}
        )
    pulls.append({"number": child["pr_number"], "merged": False, "node_id": "node"})
    monkeypatch.setattr(r.landing, "_queued_ids", lambda _ids: set())
    refusals = []
    monkeypatch.setattr(
        r.landing, "_refuse", lambda _item, reason: refusals.append(reason)
    )
    assert r.landing._enforce_landing_hold(child["repo"], child)
    assert refusals == ["receipt_landing_held"]
