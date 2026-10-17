"""Cover Astra planner routing when Astra is already a reviewer fallback."""

from __future__ import annotations

import json

import pytest
from shared.testing.plugin import _find_migrations_dir
from sqlalchemy import text

from factory.orchestration import factory_policy_restore_astra_postgres_test as restore
from factory.orchestration.factory_controls import validate_policy
from factory.orchestration.factory_models import FactoryReceipt
from factory.orchestration.model_pool import select_model

MIGRATION = "20261017100000_factory_policy_astra_planner.sql"
ACTOR = "migration:20261017100000_factory_policy_astra_planner"

LIVE_POOLS = {
    "conductor": ["opus", "sol"],
    "worker": ["sol", "spark", "sonnet"],
    "implement": ["sol", "spark", "sonnet"],
    "refine": ["sol", "spark"],
    "reviewer": ["opus", "astra", "sol"],
}


def _live_policy(**overrides):
    """Receipt 833's supplied generation-14 effective policy, without defaults."""
    policy = {
        "repo": "jomcgi-org/homelab",
        "generation": 14,
        "issue_numbers": [6288],
        "allowed_models": ["astra", "opus", "sol", "sonnet", "spark"],
        "conductor_model": "opus",
        "worker_model": "sol",
        "reviewer_model": "opus",
        "model_pools": LIVE_POOLS,
        "auto_merge": True,
        "base_branch": "main",
        "max_attempts": 2,
        "max_parallel_nodes": 1,
        "max_planner_turns": 9,
        "max_task_turns_hard": 9,
        "max_turns_per_task": 9,
        "max_review_rounds": 4,
        "max_review_recovery_rounds": 1,
        "max_tasks": {"delivery": 10, "advisory": 2},
        "task_budget_usd": 50,
        "turn_budget_usd": 4,
        "task_timeout_seconds": 43200,
        "turn_timeout_seconds": 14400,
        "progress_watchdog": {"enabled": True, "threshold_usd": 20},
        "quota_guard": {
            "claude_7d_pause_percent": 85,
            "claude_7d_resume_percent": 75,
            "claude_7d_imminent_reset_minutes": 120,
        },
        "intake": {
            "enabled": True,
            "refine_enabled": True,
            "close_enabled": True,
            "labels": ["agent-ready"],
            "exclude_labels": ["needs-human", "security-finding", "wontfix"],
            "cooldown_hours": 24,
            "max_per_day": 50,
            "max_closes_per_day": 10,
            "dependency_pr_authors": [],
            "dependency_pr_author_ids": [],
        },
        "problem_issues": {
            "enabled": True,
            "labels": ["bug"],
            "issue_pages": 2,
            "issues_per_page": 100,
            "max_per_24_hours": 3,
            "max_per_tick": 1,
            "retry_minutes": [2, 4, 8, 16, 32, 60],
            "source_audit_limit": 50,
            "sources": {
                "landing_recovery_exhausted": False,
                "node_stalled": False,
                "workflow_stranded": True,
            },
        },
    }
    policy.update(overrides)
    return policy


def _all_audits(session):
    return session.execute(text("SELECT * FROM swarm.factory_audit ORDER BY id")).all()


def test_070000_refuses_the_live_reviewer_fallback_shape(session):
    restore._seed(session, _live_policy())
    before = restore._control(session)
    audits = _all_audits(session)
    restore._run(session)
    assert restore._control(session) == before
    assert _all_audits(session) == audits


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


def test_live_shape_changes_only_routing_and_audits_once(session):
    before = _live_policy()
    restore._seed(session, before)
    audits = _all_audits(session)
    restore._run(session)
    _run(session)

    control = restore._control(session)
    after = json.loads(control.policy_json)
    expected_pools = {
        **LIVE_POOLS,
        "conductor": ["astra", "opus", "sol"],
        "refine": ["astra", "sol", "spark"],
        "worker": ["sol", "spark", "sonnet", "astra"],
        "implement": ["sol", "spark", "sonnet", "astra"],
    }
    assert after == {
        **before,
        "conductor_model": "astra",
        "model_pools": expected_pools,
    }
    # Validation also accepts the preserved legacy keys and operational bounds.
    assert validate_policy(after)["model_pools"] == expected_pools
    assert (control.version, control.actor, control.state) == (150, ACTOR, "enabled")
    new_audits = _all_audits(session)
    assert new_audits[:-1] == audits
    audit = new_audits[-1]._mapping
    assert (audit["actor"], audit["action"], audit["task_id"]) == (
        ACTOR,
        "policy_migrated",
        None,
    )
    routing_keys = ("conductor_model", "allowed_models", "model_pools")
    assert json.loads(audit["detail_json"]) == {
        "migration": MIGRATION.removesuffix(".sql"),
        "version": 150,
        "before": {key: before[key] for key in routing_keys},
        "after": {key: after[key] for key in routing_keys},
    }


def test_pinned_receipts_and_accounting_are_unchanged(session):
    before = _live_policy()
    restore._seed(session, before)
    receipts = [
        FactoryReceipt(
            id=receipt_id,
            repo="jomcgi-org/homelab",
            issue_number=receipt_id,
            generation=14,
            title="Pinned prior admission",
            body="Keep policy and consumed allowance",
            url=f"https://github.com/jomcgi-org/homelab/issues/{receipt_id}",
            actor="factory:intake",
            state=state,
            policy_json=json.dumps(before),
            allowance_json='{"turns":17,"budget_usd":50,"spent_usd":23}',
            escalation_json='{"reason":"awaiting decision"}'
            if state == "escalated"
            else None,
        )
        for receipt_id, state in ((822, "admitted"), (824, "escalated"))
    ]
    session.add_all(receipts)
    session.flush()
    pinned = [receipt.model_dump() for receipt in receipts]
    restore._run(session)
    _run(session)
    for receipt, original in zip(receipts, pinned):
        session.refresh(receipt)
        assert receipt.model_dump() == original


def test_quota_selection_preserves_role_preferences(session, monkeypatch):
    monkeypatch.delenv("SWARM_QUOTA_FLOORS", raising=False)
    restore._seed(session, _live_policy())
    _run(session)
    policy = json.loads(restore._control(session).policy_json)
    available = {"codex": {"observed": True, "exhausted": False, "age_seconds": 0}}
    for role, model in (
        ("conductor", "astra"),
        ("refine", "astra"),
        ("worker", "sol"),
        ("implement", "sol"),
        ("reviewer", "opus"),
    ):
        assert select_model(role, policy, quota=available)["model"] == model
    walled = {"codex": {"observed": True, "exhausted": True, "age_seconds": 0}}
    conductor = select_model("conductor", policy, quota=walled)
    assert conductor["model"] == "opus"
    assert conductor["fallback_from"] == "astra"
    refine = select_model("refine", policy, quota=walled)
    assert refine["model"] == "spark"
    assert [skip["model"] for skip in refine["skipped"]] == ["astra", "sol"]


def test_rerun_changes_no_control_columns_or_audits(session):
    restore._seed(session, _live_policy())
    _run(session)
    before = session.execute(text("SELECT * FROM swarm.factory_control")).all()
    audits = _all_audits(session)
    _run(session)
    assert session.execute(text("SELECT * FROM swarm.factory_control")).all() == before
    assert _all_audits(session) == audits


def test_070000_fixture_is_already_restored_before_followup(session):
    restore._seed(session, restore._policy())
    restore._run(session)
    before = restore._control(session)
    audits = _all_audits(session)
    assert json.loads(before.policy_json)["conductor_model"] == "astra"
    assert before.actor == restore.ACTOR
    _run(session)
    assert restore._control(session) == before
    assert _all_audits(session) == audits


@pytest.mark.parametrize(
    "overrides",
    [
        {"generation": 15},
        {"conductor_model": "sol"},
        {"worker_model": "spark"},
        {"reviewer_model": "sol"},
        {"repo": "another/repository"},
        {"allowed_models": ["astra", "sol", "sonnet", "spark"]},
        {"allowed_models": ["astra", "opus", "sonnet", "spark"]},
        {"model_pools": {**LIVE_POOLS, "conductor": ["opus", "astra", "sol"]}},
        {"model_pools": None},
        {"model_pools": {"worker": ["sol"] * 8}},
        {
            "allowed_models": ["astra", "opus", "sol"]
            + [f"other-{i}" for i in range(13)]
        },
    ],
)
def test_guard_preserves_operator_changes_and_size_bounds(session, overrides):
    restore._seed(session, _live_policy(**overrides))
    before = restore._control(session)
    audits = _all_audits(session)
    _run(session)
    assert restore._control(session) == before
    assert _all_audits(session) == audits


@pytest.mark.parametrize("has_spark", [True, False])
def test_missing_pools_use_070000_defaults_and_preserve_judge(session, has_spark):
    allowed = ["astra", "opus", "sol", "sonnet"] + (["spark"] if has_spark else [])
    before = _live_policy(
        allowed_models=allowed,
        model_pools={"worker": ["sol", "sonnet"], "judge": ["opus", "sol"]},
    )
    restore._seed(session, before)
    _run(session)
    after = json.loads(restore._control(session).policy_json)
    validate_policy(after)
    assert after["model_pools"] == {
        "conductor": ["astra", "opus", "sol"],
        "worker": ["sol", "sonnet", "astra"],
        "implement": ["sol", "sonnet", "astra"],
        "reviewer": ["opus", "astra"],
        "refine": ["astra", "spark", "sol"] if has_spark else ["astra", "sol"],
        "judge": ["opus", "sol"],
    }
    assert after["allowed_models"] == sorted(allowed)


def test_existing_astra_members_keep_order_without_duplicates(session):
    before = _live_policy(
        allowed_models=["spark", "sol", "astra", "opus", "astra", "sonnet"],
        model_pools={
            **LIVE_POOLS,
            "refine": ["sol", "astra", "spark", "sol"],
            "worker": ["sol", "astra", "spark", "sonnet"],
            "implement": ["sol", "spark", "astra", "sonnet"],
            "judge": ["sol", "opus"],
        },
    )
    restore._seed(session, before)
    _run(session)
    after = json.loads(restore._control(session).policy_json)
    validate_policy(after)
    assert after["allowed_models"] == ["astra", "opus", "sol", "sonnet", "spark"]
    assert after["model_pools"] == {
        **before["model_pools"],
        "conductor": ["astra", "opus", "sol"],
        "refine": ["astra", "sol", "spark"],
    }
