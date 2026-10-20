"""Keep the daily admission migration separate from every other factory control."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from factory.orchestration.factory_controls import repos_map, validate_policy
from factory.orchestration.factory_models import FactoryReceipt
from shared.testing.plugin import _find_migrations_dir

MIGRATION = "20261019021500_factory_daily_admission_limit"
ACTOR = f"migration:{MIGRATION}"


def _policy():
    return validate_policy(
        {
            "repo": "jomcgi-org/homelab",
            "issue_numbers": [6288],
            "generation": 14,
            "max_tasks": {"delivery": 10, "advisory": 2},
            "task_budget_usd": 50,
            "turn_budget_usd": 4,
            "max_attempts": 2,
            "max_parallel_nodes": 1,
            "max_task_turns_hard": 9,
            "allowed_models": ["astra", "opus", "sol", "sonnet", "spark"],
            "conductor_model": "astra",
            "worker_model": "astra",
            "reviewer_model": "opus",
            "base_branch": "main",
            "turn_timeout_seconds": 14400,
            "task_timeout_seconds": 43200,
            "intake": {
                "enabled": True,
                "refine_enabled": True,
                "close_enabled": True,
                "max_per_day": 50,
                "max_closes_per_day": 10,
            },
            "repos": {"weave-hand/loom": {"enabled": False, "max_per_day": 2}},
        }
    )


def _seed(session, policy, state="enabled"):
    session.execute(
        text(
            "UPDATE swarm.factory_control SET policy_json = :policy, "
            "state = :state, version = 169, actor = 'operator' WHERE id = 'factory'"
        ),
        {"policy": json.dumps(policy), "state": state},
    )


def _run(session):
    session.execute(text((_find_migrations_dir() / f"{MIGRATION}.sql").read_text()))


def _control(session):
    return session.execute(
        text("SELECT * FROM swarm.factory_control WHERE id = 'factory'")
    ).one()


def _audits(session):
    return session.execute(text("SELECT * FROM swarm.factory_audit ORDER BY id")).all()


@pytest.mark.parametrize("state", ["enabled", "paused", "stopped"])
def test_only_daily_limit_changes_with_one_audit_and_pinned_receipts(session, state):
    before = _policy()
    _seed(session, before, state)
    receipt = FactoryReceipt(
        repo="jomcgi-org/homelab",
        issue_number=6288,
        generation=14,
        title="Pinned admission",
        body="Preserve existing policy and accounting",
        url="https://github.com/jomcgi-org/homelab/issues/6288",
        actor="factory:intake",
        state="escalated",
        policy_json=json.dumps(before),
        allowance_json='{"turns":9,"budget_usd":50,"spent_usd":23}',
        escalation_json='{"reason":"awaiting decision"}',
    )
    session.add(receipt)
    session.flush()
    pinned = receipt.model_dump()
    audits = _audits(session)

    _run(session)

    control = _control(session)
    after = json.loads(control.policy_json)
    assert after == {**before, "intake": {**before["intake"], "max_per_day": 500}}
    assert validate_policy(after) == after
    assert repos_map(after)["jomcgi-org/homelab"]["max_per_day"] == 500
    assert repos_map(after)["weave-hand/loom"] == repos_map(before)["weave-hand/loom"]
    assert (control.version, control.actor, control.state) == (170, ACTOR, state)
    session.refresh(receipt)
    assert receipt.model_dump() == pinned
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
        "version": 170,
        "before": {"intake": {"max_per_day": 50}},
        "after": {"intake": {"max_per_day": 500}},
    }

    _run(session)
    assert _control(session) == control
    assert _audits(session) == new_audits


@pytest.mark.parametrize("cap", [None, 5, 100, 500, "50"])
def test_operator_changed_or_missing_caps_are_untouched(session, cap):
    policy = _policy()
    if cap is None:
        del policy["intake"]["max_per_day"]
    else:
        policy["intake"]["max_per_day"] = cap
    _seed(session, policy)
    before, audits = _control(session), _audits(session)
    _run(session)
    assert _control(session) == before
    assert _audits(session) == audits


def test_other_primary_repository_is_untouched(session):
    policy = _policy()
    policy["repo"] = "other/repository"
    _seed(session, policy)
    before, audits = _control(session), _audits(session)
    _run(session)
    assert _control(session) == before
    assert _audits(session) == audits
