"""PostgreSQL coverage for the one-shot factory policy migration off astra.

The migration rewrites the TEXT policy through jsonb, which SQLite cannot
exercise, so it is replayed here against a realistic live policy.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from factory.orchestration.factory_controls import validate_policy
from shared.testing.plugin import _find_migrations_dir

MIGRATION = "20260924060000_factory_policy_no_astra.sql"
ACTOR = "migration:20260924060000_factory_policy_no_astra"
POOLS = {
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
    """The FACTORY.md example, shaped like the live astra policy."""
    policy = {
        "repo": "jomcgi-org/homelab",
        "issue_numbers": [1234],
        "generation": 13,
        "max_tasks": {"delivery": 1, "advisory": 1},
        "max_task_turns_hard": 40,
        "max_parallel_nodes": 1,
        "task_budget_usd": 60,
        "turn_budget_usd": 5,
        "allowed_models": ["astra", "luna", "opus", "sol", "sonnet", "spark"],
        "conductor_model": "astra",
        "worker_model": "sol",
        "reviewer_model": "opus",
        "model_pools": {"conductor": ["astra", "spark"], "worker": ["sol", "sonnet"]},
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
            "WHERE action = 'policy_migrated' ORDER BY id"
        )
    ).all()


def test_the_migration_moves_the_live_policy_off_astra(session):
    before = _live_policy()
    _seed(session, before)

    session.execute(text(_migration()))

    control = _control(session)
    after = json.loads(control.policy_json)
    assert after["conductor_model"] == "opus"
    assert after["worker_model"] == "spark"
    assert after["reviewer_model"] == "opus"
    assert after["allowed_models"] == ["luna", "opus", "sol", "sonnet", "spark"]
    assert after["model_pools"] == POOLS
    untouched = {
        key: value
        for key, value in before.items()
        if key
        not in {
            "conductor_model",
            "worker_model",
            "reviewer_model",
            "allowed_models",
            "model_pools",
        }
    }
    assert {key: after[key] for key in untouched} == untouched
    assert set(after) == set(before)
    assert after["generation"] == 13
    assert control.version == 8
    assert control.actor == ACTOR
    assert control.state == "enabled"

    assert validate_policy(after) == after

    [audit] = _audits(session)
    assert audit.actor == ACTOR
    assert audit.task_id is None
    detail = json.loads(audit.detail_json)
    assert detail["version"] == 8
    assert detail["before"]["conductor_model"] == "astra"
    assert detail["before"]["worker_model"] == "sol"
    assert detail["before"]["reviewer_model"] == "opus"
    assert detail["before"]["model_pools"] == before["model_pools"]
    assert detail["after"]["conductor_model"] == "opus"
    assert detail["after"]["worker_model"] == "spark"
    assert detail["after"]["reviewer_model"] == "opus"
    assert detail["after"]["allowed_models"] == after["allowed_models"]
    assert detail["after"]["model_pools"] == POOLS


def test_a_second_run_changes_nothing(session):
    _seed(session, _live_policy())
    session.execute(text(_migration()))
    first = _control(session)

    session.execute(text(_migration()))

    assert _control(session) == first
    assert len(_audits(session)) == 1


def test_a_policy_without_astra_is_untouched(session):
    policy = _live_policy(
        allowed_models=["luna", "opus", "sol"],
        conductor_model="opus",
        worker_model="luna",
        model_pools={"conductor": ["opus"], "worker": ["luna", "sol"]},
    )
    _seed(session, policy)
    before = _control(session)

    session.execute(text(_migration()))

    assert _control(session) == before
    assert _audits(session) == []


def test_an_astra_allowance_alone_still_triggers_the_move(session):
    _seed(
        session,
        _live_policy(
            conductor_model="opus",
            model_pools={"conductor": ["opus", "astra"], "worker": ["sol"]},
        ),
    )

    session.execute(text(_migration()))

    after = json.loads(_control(session).policy_json)
    assert "astra" not in after["allowed_models"]
    assert after["model_pools"] == POOLS
    assert validate_policy(after) == after
