"""Exercise the guarded Terra review migration against PostgreSQL and routing."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from shared.testing.plugin import _find_migrations_dir
from sqlalchemy import text
from sqlmodel import create_engine

from factory.orchestration.factory_controls import validate_policy
from factory.orchestration.factory_models import FactoryReceipt
from factory.orchestration.model_pool import effort_for, pool_for, select_reviewer

MIGRATION = "20261106090000_factory_terra_reviews"
ACTOR = f"migration:{MIGRATION}"
POOLS = {
    "conductor": ["astra", "opus", "sol"],
    "worker": ["astra", "sol", "spark", "sonnet"],
    "implement": ["sol", "astra", "spark", "sonnet"],
    "reviewer": ["opus", "astra", "sol"],
    "refine": ["astra", "sol", "spark"],
}


def _policy():
    return validate_policy(
        {
            "repo": "jomcgi-org/homelab",
            "issue_numbers": [6471],
            "generation": 14,
            "max_tasks": {"delivery": 10, "advisory": 2},
            "max_task_turns_hard": 9,
            "max_parallel_nodes": 1,
            "task_budget_usd": 50,
            "turn_budget_usd": 4,
            "allowed_models": ["astra", "opus", "sol", "sonnet", "spark"],
            "conductor_model": "astra",
            "worker_model": "astra",
            "reviewer_model": "opus",
            "model_pools": deepcopy(POOLS),
            "role_effort": {
                "planner": "xhigh",
                "reviewer": "xhigh",
                "implement": "medium",
                "integrate": "xhigh",
                "correct": "xhigh",
            },
            "base_branch": "main",
            "turn_timeout_seconds": 14400,
            "task_timeout_seconds": 43200,
            "max_attempts": 2,
            "max_review_rounds": 4,
            "intake": {"enabled": True, "refine_enabled": True, "max_per_day": 500},
            "auto_merge": True,
        }
    )


def _seed(db, policy, *, state="enabled", version=331):
    db.execute(
        text(
            "UPDATE swarm.factory_control SET policy_json = :policy, "
            "state = :state, version = :version, actor = 'operator', "
            "updated_at = '2026-11-01T00:00:00Z' WHERE id = 'factory'"
        ),
        {"policy": json.dumps(policy), "state": state, "version": version},
    )


def _run(db):
    db.execute(text((_find_migrations_dir() / f"{MIGRATION}.sql").read_text()))


def _control(db):
    return db.execute(
        text("SELECT * FROM swarm.factory_control WHERE id = 'factory'")
    ).one()


def _audits(db):
    return db.execute(text("SELECT * FROM swarm.factory_audit ORDER BY id")).all()


def _expected(before):
    return {
        **before,
        "reviewer_model": "terra",
        "allowed_models": before["allowed_models"]
        if "terra" in before["allowed_models"]
        else [*before["allowed_models"], "terra"],
        "model_pools": {
            **before["model_pools"],
            "reviewer": ["terra", "opus", "astra", "sol"],
        },
        "role_effort": {**before["role_effort"], "reviewer": "xhigh"},
    }


def _changed(policy):
    return {
        key: policy[key]
        for key in ("reviewer_model", "allowed_models", "model_pools", "role_effort")
    }


@pytest.mark.parametrize("state", ["enabled", "paused", "stopped"])
@pytest.mark.parametrize("version", [331, 412])
def test_only_future_review_routing_changes_with_one_audit(session, state, version):
    before = _policy()
    _seed(session, before, state=state, version=version)
    receipt = FactoryReceipt(
        repo="jomcgi-org/homelab",
        issue_number=6471,
        generation=14,
        title="Pinned review admission",
        body="Preserve routing, holds and accounting",
        url="https://github.com/jomcgi-org/homelab/issues/6471",
        actor="factory:intake",
        state="admitted",
        task_paused=True,
        policy_json=json.dumps(before),
        allowance_json='{"turns":9,"budget_usd":50,"spent_usd":23}',
        escalation_json='{"reason":"awaiting decision"}',
        direction_json='{"landing_hold":{"actor":"operator"}}',
    )
    session.add(receipt)
    session.flush()
    pinned = receipt.model_dump()
    prior_control = _control(session)
    audits = _audits(session)

    _run(session)

    control = _control(session)
    after = json.loads(control.policy_json)
    assert after == _expected(before)
    assert validate_policy(after) == after
    assert (control.version, control.actor, control.state) == (
        version + 1,
        ACTOR,
        state,
    )
    assert control.updated_at > prior_control.updated_at
    session.refresh(receipt)
    assert receipt.model_dump() == pinned
    assert select_reviewer(after, window_high=False, quota={})["model"] == "terra"
    assert select_reviewer(before, window_high=False, quota={})["model"] == "opus"
    assert effort_for("review_delivery", after) == "xhigh"
    assert (
        select_reviewer(after, window_high=False, judgment=True, quota={})["model"]
        == "opus"
    )
    assert pool_for("judge", after) == ["opus"]
    for role in ("conductor", "worker", "implement", "refine"):
        assert pool_for(role, after) == pool_for(role, before)
    new_audits = _audits(session)
    assert new_audits[:-1] == audits
    audit = new_audits[-1]
    assert (audit.actor, audit.action, audit.task_id) == (
        ACTOR,
        "policy_migrated",
        None,
    )
    assert json.loads(audit.detail_json) == {
        "migration": MIGRATION,
        "version": version + 1,
        "before": _changed(before),
        "after": _changed(after),
    }

    _run(session)
    assert _control(session) == control
    assert _audits(session) == new_audits


@pytest.mark.parametrize(
    "change",
    [
        "repo",
        "generation",
        "conductor",
        "worker",
        "reviewer",
        "pool_order",
        "missing_pool",
        "effort",
        "missing_effort",
        "allowed_models",
    ],
)
def test_operator_routing_drift_is_not_overwritten(session, change):
    policy = _policy()
    if change == "repo":
        policy["repo"] = "another/repository"
    elif change == "generation":
        policy["generation"] = 15
    elif change in {"conductor", "worker", "reviewer"}:
        policy[f"{change}_model"] = "sol"
    elif change == "pool_order":
        policy["model_pools"]["reviewer"] = ["opus", "sol", "astra"]
    elif change == "missing_pool":
        del policy["model_pools"]["reviewer"]
    elif change == "effort":
        policy["role_effort"]["reviewer"] = "high"
    elif change == "missing_effort":
        del policy["role_effort"]["reviewer"]
    else:
        policy["allowed_models"].remove("sonnet")
    _seed(session, policy)
    before, audits = _control(session), _audits(session)
    _run(session)
    assert _control(session) == before
    assert _audits(session) == audits


@pytest.mark.parametrize(
    "count,has_terra,changes",
    [
        (15, False, True),
        (16, False, False),
        (16, True, True),
        (17, False, False),
        (17, True, False),
    ],
)
def test_allowed_model_capacity_is_preserved(session, count, has_terra, changes):
    policy = _policy()
    if has_terra:
        policy["allowed_models"].append("terra")
    policy["allowed_models"].extend(
        f"custom-{number}" for number in range(count - len(policy["allowed_models"]))
    )
    assert len(policy["allowed_models"]) == count
    _seed(session, policy)
    before, audits = _control(session), _audits(session)
    _run(session)
    if changes:
        after = json.loads(_control(session).policy_json)
        assert after == _expected(policy)
        assert len(after["allowed_models"]) == 16
        assert after["allowed_models"].count("terra") == 1
        assert len(_audits(session)) == len(audits) + 1
    else:
        assert _control(session) == before
        assert _audits(session) == audits


@pytest.mark.parametrize("models", [None, {}, "astra sol spark sonnet opus"])
def test_non_array_allowed_models_is_a_guard_miss(session, models):
    policy = {**_policy(), "allowed_models": models}
    _seed(session, policy)
    before, audits = _control(session), _audits(session)
    _run(session)
    assert _control(session) == before
    assert _audits(session) == audits


def test_unrelated_policy_and_refill_history_survive(session):
    before = _policy()
    before["task_budget_usd"] = 70
    before["intake"]["enabled"] = False
    before["model_pools"]["judge"] = ["opus"]
    before["role_effort"]["funding"] = "max"
    before["backlog_refill"] = {"enabled": False, "cooldown_minutes": 90}
    _seed(session, before)
    session.execute(
        text(
            "INSERT INTO swarm.factory_audit (actor, action, detail_json) VALUES "
            "('factory:backlog-refill', 'backlog_refill_failed', :detail)"
        ),
        {"detail": json.dumps({"attempt": 3})},
    )
    audits = _audits(session)
    _run(session)
    assert json.loads(_control(session).policy_json) == _expected(before)
    assert _audits(session)[:-1] == audits


@pytest.mark.parametrize("routing_changed", [False, True])
def test_concurrent_operator_edit_is_rechecked_after_the_row_lock(pg, routing_changed):
    engine = create_engine(pg.url)
    with engine.begin() as db:
        original = dict(_control(db)._mapping)
        audit_floor = db.execute(
            text("SELECT COALESCE(MAX(id), 0) FROM swarm.factory_audit")
        ).scalar_one()
        _seed(db, _policy())
    edited = _policy()
    edited["task_budget_usd"] = 73
    if routing_changed:
        edited["reviewer_model"] = "astra"
        edited["model_pools"]["reviewer"] = ["astra", "opus", "sol"]
    started = threading.Event()
    backend = []

    def migrate():
        with engine.begin() as db:
            db.execute(text("SET LOCAL statement_timeout = '8s'"))
            backend.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            started.set()
            _run(db)

    try:
        with engine.connect() as editor, ThreadPoolExecutor(max_workers=1) as workers:
            transaction = editor.begin()
            _seed(editor, edited, version=412)
            future = workers.submit(migrate)
            try:
                assert started.wait(3)
                blocked = False
                deadline = time.monotonic() + 3
                with engine.connect() as observer:
                    while time.monotonic() < deadline and not future.done():
                        blocked = observer.execute(
                            text(
                                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                            ),
                            {"pid": backend[0]},
                        ).scalar_one()
                        observer.rollback()
                        if blocked:
                            break
                        time.sleep(0.02)
                assert blocked, (
                    "migration must wait for the concurrent control-row edit"
                )
                transaction.commit()
            finally:
                if transaction.is_active:
                    transaction.rollback()
            future.result(timeout=5)
        with engine.connect() as db:
            control = _control(db)
            assert json.loads(control.policy_json) == (
                edited if routing_changed else _expected(edited)
            )
            assert control.version == (412 if routing_changed else 413)
            new_audits = db.execute(
                text("SELECT * FROM swarm.factory_audit WHERE id > :floor ORDER BY id"),
                {"floor": audit_floor},
            ).all()
            assert len(new_audits) == (0 if routing_changed else 1)
    finally:
        with engine.begin() as db:
            db.execute(
                text(
                    "UPDATE swarm.factory_control SET state = :state, policy_json = :policy_json, "
                    "version = :version, actor = :actor, updated_at = :updated_at WHERE id = 'factory'"
                ),
                original,
            )
            db.execute(
                text(
                    "DELETE FROM swarm.factory_audit WHERE actor = :actor AND id > :floor"
                ),
                {"actor": ACTOR, "floor": audit_floor},
            )
        engine.dispose()
