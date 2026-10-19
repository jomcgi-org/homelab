"""Exercise the narrow Sol-first migration against PostgreSQL and routing."""

from __future__ import annotations

import json

import pytest
from shared.testing.plugin import _find_migrations_dir
from sqlalchemy import text

from factory.orchestration.factory_controls import validate_policy
from factory.orchestration.factory_models import FactoryReceipt
from factory.orchestration.model_pool import effort_for, select_model

MIGRATION = "20261019093000_factory_implement_sol_first"
ACTOR = f"migration:{MIGRATION}"
POOLS = {
    "conductor": ["astra", "opus", "sol"],
    "worker": ["astra", "sol", "spark", "sonnet"],
    "implement": ["astra", "sol", "spark", "sonnet"],
    "reviewer": ["opus", "astra", "sol"],
    "refine": ["astra", "sol", "spark"],
}


def _policy(**overrides):
    policy = {
        "repo": "jomcgi-org/homelab",
        "issue_numbers": [6288],
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
        "model_pools": POOLS,
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
    policy.update(overrides)
    return validate_policy(policy)


def _seed(session, policy, state="enabled"):
    session.execute(
        text(
            "UPDATE swarm.factory_control SET policy_json = :policy, "
            "state = :state, version = 171, actor = 'operator' WHERE id = 'factory'"
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
def test_only_implement_order_changes_with_one_audit_and_pinned_receipts(
    session, state
):
    before = _policy()
    _seed(session, before, state)
    receipt = FactoryReceipt(
        repo="jomcgi-org/homelab",
        issue_number=6288,
        generation=14,
        title="Pinned admission",
        body="Preserve existing routing and accounting",
        url="https://github.com/jomcgi-org/homelab/issues/6288",
        actor="factory:intake",
        state="admitted",
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
    assert after == {
        **before,
        "model_pools": {**POOLS, "implement": ["sol", "astra", "spark", "sonnet"]},
    }
    assert validate_policy(after) == after
    assert (control.version, control.actor, control.state) == (172, ACTOR, state)
    session.refresh(receipt)
    assert receipt.model_dump() == pinned
    assert select_model("implement", after, quota={})["model"] == "sol"
    assert select_model("implement", before, quota={})["model"] == "astra"
    for role in ("conductor", "worker", "reviewer", "refine"):
        assert select_model(role, after, quota={}) == select_model(
            role, before, quota={}
        )
    assert effort_for("implement_work", after) == "medium"
    assert effort_for("implement_complex", after, "xhigh") == "xhigh"
    walled = {"codex": {"observed": True, "exhausted": True, "age_seconds": 0}}
    assert select_model("implement", after, quota=walled)["model"] == "spark"
    new_audits = _audits(session)
    assert new_audits[:-1] == audits
    audit = new_audits[-1]._mapping
    assert (audit["actor"], audit["action"], audit["task_id"]) == (
        ACTOR,
        "policy_migrated",
        None,
    )
    assert json.loads(audit["detail_json"]) == {
        "migration": MIGRATION,
        "version": 172,
        "before": {"model_pools": before["model_pools"]},
        "after": {"model_pools": after["model_pools"]},
    }

    _run(session)
    assert _control(session) == control
    assert _audits(session) == new_audits


@pytest.mark.parametrize(
    "overrides",
    [
        {"repo": "another/repository"},
        {"generation": 15},
        {
            "conductor_model": "opus",
            "model_pools": {**POOLS, "conductor": ["opus", "astra", "sol"]},
        },
        {
            "worker_model": "sol",
            "model_pools": {**POOLS, "worker": ["sol", "astra", "spark", "sonnet"]},
        },
        {
            "reviewer_model": "astra",
            "model_pools": {**POOLS, "reviewer": ["astra", "opus", "sol"]},
        },
        {
            "model_pools": {
                role: pool for role, pool in POOLS.items() if role != "implement"
            }
        },
        {"model_pools": {**POOLS, "implement": ["astra", "spark", "sol", "sonnet"]}},
        {"model_pools": {**POOLS, "implement": ["sol", "astra", "spark", "sonnet"]}},
    ],
)
def test_different_operator_routing_is_not_overwritten(session, overrides):
    _seed(session, _policy(**overrides))
    control, audits = _control(session), _audits(session)
    _run(session)
    assert _control(session) == control
    assert _audits(session) == audits


def test_unrelated_operator_customizations_are_preserved(session):
    before = _policy(
        model_pools={**POOLS, "worker": ["astra", "spark"], "judge": ["opus"]},
        role_effort={"implement": "xhigh", "funding": "max"},
        task_budget_usd=70,
        intake={"enabled": False, "max_per_day": 123},
    )
    _seed(session, before)
    _run(session)
    after = json.loads(_control(session).policy_json)
    assert after == {
        **before,
        "model_pools": {
            **before["model_pools"],
            "implement": ["sol", "astra", "spark", "sonnet"],
        },
    }
