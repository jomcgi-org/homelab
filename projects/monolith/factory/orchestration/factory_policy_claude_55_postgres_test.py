"""PostgreSQL coverage for the staged Claude 5.5 factory policy migration.

The migration rewrites the TEXT policy through jsonb, which SQLite cannot
exercise, so it is replayed here against the live policy's shape.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from factory.orchestration.factory_controls import validate_policy
from shared.testing.plugin import _find_migrations_dir

MIGRATION = "20260930060100_factory_policy_claude_55.sql"
ACTOR = "migration:20260930060100_factory_policy_claude_55"
LIVE_POOLS = {
    "conductor": ["opus", "sol"],
    "worker": ["spark", "sol"],
    "implement": ["spark", "sol"],
    "reviewer": ["opus"],
    "refine": ["spark", "sol"],
}


def _migration() -> str:
    source = (_find_migrations_dir() / MIGRATION).read_text()
    return "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("--")
    ).strip()


def _live_policy(**overrides) -> dict:
    """The policy the astra migration left behind, at a $36 task budget."""
    policy = {
        "repo": "jomcgi-org/homelab",
        "issue_numbers": [1234],
        "generation": 13,
        "max_tasks": {"delivery": 10, "advisory": 2},
        "max_task_turns_hard": 9,
        "max_parallel_nodes": 1,
        "task_budget_usd": 36,
        "turn_budget_usd": 5,
        "allowed_models": ["luna", "opus", "sol", "spark"],
        "conductor_model": "opus",
        "worker_model": "spark",
        "reviewer_model": "opus",
        "model_pools": LIVE_POOLS,
        "base_branch": "main",
        "turn_timeout_seconds": 900,
        "task_timeout_seconds": 14400,
        "max_attempts": 2,
        "max_review_rounds": 2,
        "intake": {"enabled": True, "refine_enabled": True},
        "auto_merge": False,
    }
    policy.update(overrides)
    return validate_policy(policy)


def _seed(session, policy: dict, version: int = 7) -> None:
    session.execute(
        text(
            "UPDATE swarm.factory_control SET state = 'enabled', "
            "policy_json = :policy, version = :version, actor = 'operator' "
            "WHERE id = 'factory'"
        ),
        {"policy": json.dumps(policy), "version": version},
    )


def _control(session):
    return session.execute(
        text(
            "SELECT policy_json, version, actor, state FROM swarm.factory_control "
            "WHERE id = 'factory'"
        )
    ).one()


def _audits(session):
    return session.execute(
        text(
            "SELECT actor, action, task_id, detail_json FROM swarm.factory_audit "
            "WHERE action = 'policy_migrated' AND actor = :actor ORDER BY id"
        ),
        {"actor": ACTOR},
    ).all()


def test_the_migration_stages_sonnet_after_spark_and_raises_the_budget(session):
    before = _live_policy()
    _seed(session, before)

    session.execute(text(_migration()))

    control = _control(session)
    after = json.loads(control.policy_json)
    assert after["allowed_models"] == ["luna", "opus", "sol", "sonnet", "spark"]
    assert after["model_pools"] == {
        **LIVE_POOLS,
        "worker": ["spark", "sonnet", "sol"],
        "implement": ["spark", "sonnet", "sol"],
    }
    assert after["task_budget_usd"] == 50
    # Staged: no live default moves.
    assert after["worker_model"] == "spark"
    assert after["conductor_model"] == "opus"
    assert after["reviewer_model"] == "opus"
    untouched = {
        key: value
        for key, value in before.items()
        if key not in {"allowed_models", "model_pools", "task_budget_usd"}
    }
    assert {key: after[key] for key in untouched} == untouched
    assert set(after) == set(before)
    assert control.version == 8
    assert control.actor == ACTOR

    assert validate_policy(after) == after

    [audit] = _audits(session)
    assert audit.task_id is None
    detail = json.loads(audit.detail_json)
    assert detail["version"] == 8
    assert detail["before"]["task_budget_usd"] == 36
    assert detail["after"]["task_budget_usd"] == 50
    assert detail["before"]["model_pools"] == LIVE_POOLS
    assert detail["after"]["model_pools"] == after["model_pools"]


def test_a_second_run_changes_nothing(session):
    _seed(session, _live_policy())
    session.execute(text(_migration()))
    first = _control(session)

    session.execute(text(_migration()))

    assert _control(session) == first
    assert len(_audits(session)) == 1


def test_a_policy_already_on_the_profile_is_untouched(session):
    _seed(
        session,
        _live_policy(
            allowed_models=["opus", "sol", "sonnet", "spark"],
            task_budget_usd=60,
            model_pools={"worker": ["spark", "sol"]},
        ),
    )
    before = _control(session)

    session.execute(text(_migration()))

    assert _control(session) == before
    assert _audits(session) == []


def test_a_higher_budget_is_kept_and_pools_without_spark_are_left(session):
    policy = _live_policy(
        task_budget_usd=80,
        worker_model="sol",
        model_pools={"worker": ["sol", "luna"], "conductor": ["opus"]},
    )
    _seed(session, policy)

    session.execute(text(_migration()))

    after = json.loads(_control(session).policy_json)
    assert after["task_budget_usd"] == 80
    assert after["model_pools"] == policy["model_pools"]
    assert "sonnet" in after["allowed_models"]
    assert validate_policy(after) == after


def test_a_policy_without_pools_gains_no_pools(session):
    policy = _live_policy()
    del policy["model_pools"]
    _seed(session, policy)

    session.execute(text(_migration()))

    after = json.loads(_control(session).policy_json)
    assert "model_pools" not in after
    assert "sonnet" in after["allowed_models"]
    assert after["task_budget_usd"] == 50
    assert validate_policy(after) == after
