"""Exercise the Astra restoration against PostgreSQL and real routing rules."""

from __future__ import annotations

import json

import pytest
from shared.testing.plugin import _find_migrations_dir
from sqlalchemy import text

from factory.orchestration.factory_controls import validate_policy
from factory.orchestration.factory_models import FactoryReceipt
from factory.orchestration.model_pool import select_model

MIGRATION = "20261017070000_factory_policy_restore_astra.sql"
ACTOR = "migration:20261017070000_factory_policy_restore_astra"
POOLS = {
    "conductor": ["opus", "sol"],
    "worker": ["sol", "spark", "sonnet"],
    "implement": ["sol", "spark", "sonnet"],
    "reviewer": ["opus"],
    "refine": ["sol", "spark"],
    "judge": ["opus"],
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
        "allowed_models": ["opus", "sol", "sonnet", "spark"],
        "conductor_model": "opus",
        "worker_model": "sol",
        "reviewer_model": "opus",
        "model_pools": POOLS,
        "base_branch": "main",
        "turn_timeout_seconds": 14400,
        "task_timeout_seconds": 43200,
        "max_attempts": 2,
        "max_review_rounds": 4,
        "intake": {"enabled": True, "refine_enabled": True},
        "auto_merge": True,
    }
    policy.update(overrides)
    return validate_policy(policy)


def _seed(session, policy):
    session.execute(
        text(
            "UPDATE swarm.factory_control SET state = 'enabled', "
            "policy_json = :policy, version = 149, actor = 'operator' "
            "WHERE id = 'factory'"
        ),
        {"policy": json.dumps(policy)},
    )


def _run(session):
    source = (_find_migrations_dir() / MIGRATION).read_text()
    session.execute(
        text(
            "\n".join(
                line
                for line in source.splitlines()
                if not line.strip().startswith("--")
            )
        )
    )


def _control(session):
    return session.execute(
        text(
            "SELECT policy_json, version, actor, state FROM swarm.factory_control "
            "WHERE id = 'factory'"
        )
    ).one()


def _audits(session):
    return (
        session.execute(
            text("SELECT detail_json FROM swarm.factory_audit WHERE actor = :actor"),
            {"actor": ACTOR},
        )
        .scalars()
        .all()
    )


def test_restoration_preserves_authority_and_pinned_receipts(session):
    before = _policy()
    _seed(session, before)
    receipt = FactoryReceipt(
        repo="jomcgi-org/homelab",
        issue_number=6624,
        generation=14,
        title="Existing escalated work",
        body="Keep original authority",
        url="https://github.com/jomcgi-org/homelab/issues/6624",
        actor="factory:intake",
        state="escalated",
        policy_json=json.dumps(before),
        allowance_json='{"turns":17,"budget_usd":62}',
        escalation_json='{"reason":"expired"}',
    )
    session.add(receipt)
    session.flush()
    pinned = (receipt.policy_json, receipt.allowance_json, receipt.escalation_json)

    _run(session)

    control = _control(session)
    after = json.loads(control.policy_json)
    assert validate_policy(after) == after
    assert after["conductor_model"] == "astra"
    assert after["model_pools"] == {
        **POOLS,
        "conductor": ["astra", "opus", "sol"],
        "worker": ["sol", "spark", "sonnet", "astra"],
        "implement": ["sol", "spark", "sonnet", "astra"],
        "reviewer": ["opus", "astra"],
        "refine": ["astra", "sol", "spark"],
    }
    assert after["allowed_models"] == ["astra", "opus", "sol", "sonnet", "spark"]
    changed = {"conductor_model", "model_pools", "allowed_models"}
    assert {k: v for k, v in after.items() if k not in changed} == {
        k: v for k, v in before.items() if k not in changed
    }
    assert (control.version, control.actor, control.state) == (150, ACTOR, "enabled")
    session.refresh(receipt)
    assert receipt.state == "escalated"
    assert (
        receipt.policy_json,
        receipt.allowance_json,
        receipt.escalation_json,
    ) == pinned
    [audit] = _audits(session)
    detail = json.loads(audit)
    assert detail["before"]["conductor_model"] == "opus"
    assert detail["after"]["model_pools"] == after["model_pools"]
    assert detail["version"] == 150


def test_restored_planning_uses_astra_and_retains_quota_fallback(session):
    _seed(session, _policy())
    _run(session)
    policy = json.loads(_control(session).policy_json)
    assert select_model("conductor", policy, quota={})["model"] == "astra"
    assert select_model("refine", policy, quota={})["model"] == "astra"
    assert select_model("implement", policy, quota={})["model"] == "sol"
    assert select_model("reviewer", policy, quota={})["model"] == "opus"
    walled = {"codex": {"observed": True, "exhausted": True, "age_seconds": 0}}
    assert select_model("conductor", policy, quota=walled)["model"] == "opus"


def test_rerun_does_not_change_policy_or_duplicate_audit(session):
    _seed(session, _policy())
    _run(session)
    first = _control(session)
    _run(session)
    assert _control(session) == first
    assert len(_audits(session)) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"generation": 15},
        {"allowed_models": ["astra", "opus", "sol", "sonnet", "spark"]},
        {"conductor_model": "sol", "model_pools": {**POOLS, "conductor": ["sol"]}},
        {"repo": "another/repository"},
    ],
)
def test_operator_changes_are_not_overwritten(session, overrides):
    _seed(session, _policy(**overrides))
    before = _control(session)
    _run(session)
    assert _control(session) == before
    assert _audits(session) == []


def test_missing_optional_pools_preserve_existing_fallbacks(session):
    _seed(session, _policy(model_pools={"worker": ["sol", "sonnet"]}))
    _run(session)
    after = json.loads(_control(session).policy_json)
    assert validate_policy(after) == after
    assert after["model_pools"]["implement"] == ["sol", "sonnet", "astra"]
    assert after["model_pools"]["conductor"] == ["astra", "opus", "sol"]
    assert after["model_pools"]["refine"] == ["astra", "spark", "sol"]
    walled = {"codex": {"observed": True, "exhausted": True, "age_seconds": 0}}
    assert select_model("refine", after, quota=walled)["model"] == "spark"


def test_full_pool_is_left_for_the_operator(session):
    members = ["sol", "spark", "sonnet", "opus", "luna", "terra", "fable", "pi-spark"]
    _seed(session, _policy(allowed_models=members, model_pools={"worker": members}))
    before = _control(session)
    _run(session)
    assert _control(session) == before
    assert _audits(session) == []
