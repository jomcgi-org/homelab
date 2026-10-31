"""Replay policy migrations on real PostgreSQL rows, retaining operator guards."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest
from sqlalchemy import text

from factory.orchestration.factory_controls import validate_policy
from factory.orchestration.factory_models import FactoryReceipt
from factory.orchestration.model_pool import effort_for, select_model
from shared.testing.plugin import _find_migrations_dir

MIGRATIONS = {
    "no_astra": "20260924060000_factory_policy_no_astra",
    "claude_55": "20260930060100_factory_policy_claude_55",
    "gen14_sol": "20260930210000_factory_policy_gen14_sol",
    "restore_astra": "20261017070000_factory_policy_restore_astra",
    "astra_planner": "20261017100000_factory_policy_astra_planner",
    "coding_effort": "20261017120000_factory_astra_coding_effort",
}
POOLS = {
    "conductor": ["opus", "sol"],
    "worker": ["spark", "sol"],
    "implement": ["spark", "sol"],
    "reviewer": ["opus"],
    "refine": ["spark", "sol"],
}
SOL_POOLS = {
    **POOLS,
    "worker": ["sol", "spark", "sonnet"],
    "implement": ["sol", "spark", "sonnet"],
    "refine": ["sol", "spark"],
}
PLANNER_POOLS = {**SOL_POOLS, "reviewer": ["opus", "astra", "sol"]}
EFFORT = {
    "funding": "max",
    "planner": "xhigh",
    "reviewer": "xhigh",
    "implement": "medium",
    "integrate": "xhigh",
    "correct": "xhigh",
}
AUDIT_KEYS = {
    "no_astra": (
        "conductor_model",
        "worker_model",
        "reviewer_model",
        "allowed_models",
        "model_pools",
    ),
    "claude_55": ("allowed_models", "model_pools", "task_budget_usd"),
    "gen14_sol": ("generation", "worker_model", "issue_numbers", "model_pools"),
    "restore_astra": ("conductor_model", "allowed_models", "model_pools"),
    "astra_planner": ("conductor_model", "allowed_models", "model_pools"),
    "coding_effort": ("worker_model", "model_pools", "role_effort"),
}


def _policy(profile):
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
        "model_pools": {**SOL_POOLS, "judge": ["opus"]},
        "base_branch": "main",
        "turn_timeout_seconds": 14400,
        "task_timeout_seconds": 43200,
        "max_attempts": 2,
        "max_review_rounds": 4,
        "intake": {"enabled": True, "refine_enabled": True},
        "auto_merge": True,
    }
    if profile in ("no_astra", "claude_55", "gen14_sol"):
        policy.update(generation=13, worker_model="spark", model_pools=POOLS)
    if profile == "no_astra":
        policy.update(
            allowed_models=["astra", "luna", "opus", "sol", "sonnet", "spark"],
            conductor_model="astra",
            worker_model="sol",
            model_pools={"conductor": ["astra", "spark"], "worker": ["sol", "sonnet"]},
            task_budget_usd=60,
        )
    elif profile == "claude_55":
        policy.update(
            allowed_models=["luna", "opus", "sol", "spark"], task_budget_usd=36
        )
    elif profile == "gen14_sol":
        policy.update(
            issue_numbers=[4361, 6288],
            model_pools={
                **POOLS,
                "worker": ["spark", "sonnet", "sol"],
                "implement": ["spark", "sonnet", "sol"],
            },
        )
    elif profile in ("astra_planner", "coding_effort"):
        policy.update(
            allowed_models=["astra", "opus", "sol", "sonnet", "spark"],
            model_pools=PLANNER_POOLS,
            max_planner_turns=9,
            max_turns_per_task=9,
            max_review_recovery_rounds=1,
            progress_watchdog={"enabled": True, "threshold_usd": 20},
            quota_guard={
                "claude_7d_pause_percent": 85,
                "claude_7d_resume_percent": 75,
                "claude_7d_imminent_reset_minutes": 120,
            },
        )
    if profile == "coding_effort":
        policy.update(conductor_model="astra", role_effort={"funding": "max"})
        policy["model_pools"] = _expected("astra_planner", policy)["model_pools"]
    if profile not in ("astra_planner", "coding_effort"):
        policy = validate_policy(policy)
    return deepcopy(policy)


def _expected(profile, before):
    if profile == "no_astra":
        return {
            **before,
            "conductor_model": "opus",
            "worker_model": "spark",
            "reviewer_model": "opus",
            "allowed_models": ["luna", "opus", "sol", "sonnet", "spark"],
            "model_pools": POOLS,
        }
    if profile == "claude_55":
        return {
            **before,
            "task_budget_usd": 50,
            "allowed_models": ["luna", "opus", "sol", "sonnet", "spark"],
            "model_pools": {
                **POOLS,
                "worker": ["spark", "sonnet", "sol"],
                "implement": ["spark", "sonnet", "sol"],
            },
        }
    if profile == "gen14_sol":
        return {
            **before,
            "generation": 14,
            "worker_model": "sol",
            "issue_numbers": [6288],
            "model_pools": SOL_POOLS,
        }
    pools = before["model_pools"]
    if profile == "coding_effort":
        return {
            **before,
            "worker_model": "astra",
            "role_effort": EFFORT,
            "model_pools": {
                **pools,
                "worker": ["astra", "sol", "spark", "sonnet"],
                "implement": ["astra", "sol", "spark", "sonnet"],
            },
        }
    return {
        **before,
        "conductor_model": "astra",
        "allowed_models": ["astra", "opus", "sol", "sonnet", "spark"],
        "model_pools": {
            **pools,
            "conductor": ["astra", "opus", "sol"],
            "refine": ["astra", "sol", "spark"],
            "worker": ["sol", "spark", "sonnet", "astra"],
            "implement": ["sol", "spark", "sonnet", "astra"],
            "reviewer": (
                pools["reviewer"] if profile == "astra_planner" else ["opus", "astra"]
            ),
        },
    }


def _seed(session, policy):
    session.execute(
        text(
            "UPDATE swarm.factory_control SET state = 'enabled', policy_json = :policy, "
            "version = 149, actor = 'operator' WHERE id = 'factory'"
        ),
        {"policy": json.dumps(policy)},
    )


def _run(session, profile):
    source = (_find_migrations_dir() / f"{MIGRATIONS[profile]}.sql").read_text()
    session.execute(text(source))


def _control(session):
    return session.execute(
        text("SELECT * FROM swarm.factory_control WHERE id = 'factory'")
    ).one()


def _audits(session):
    return session.execute(text("SELECT * FROM swarm.factory_audit ORDER BY id")).all()


@pytest.mark.parametrize("profile", MIGRATIONS)
def test_applied_rows_preserve_pins_audit_once_and_replay(session, profile):
    before = _policy(profile)
    _seed(session, before)
    receipts = [
        FactoryReceipt(
            repo=before["repo"],
            issue_number=number,
            generation=before["generation"],
            title="Pinned admission",
            body="Keep accounting and authority",
            url=f"https://example.test/issues/{number}",
            actor="factory:intake",
            state=state,
            policy_json=json.dumps(before),
            allowance_json='{"turns":17,"budget_usd":62,"spent_usd":23}',
            escalation_json=(
                '{"reason":"awaiting decision"}' if state == "escalated" else None
            ),
        )
        for number, state in ((822, "admitted"), (824, "escalated"))
    ]
    session.add_all(receipts)
    session.flush()
    pinned = [row.model_dump() for row in receipts]
    audits = _audits(session)
    _run(session, profile)
    control = _control(session)
    after = json.loads(control.policy_json)
    assert after == _expected(profile, before)
    validated = validate_policy(after)
    assert validated["model_pools"] == after["model_pools"]
    if profile not in ("astra_planner", "coding_effort"):
        assert validated == after
    assert (control.version, control.actor, control.state) == (
        150,
        "migration:" + MIGRATIONS[profile],
        "enabled",
    )
    current = _audits(session)
    assert current[:-1] == audits
    audit = current[-1]
    assert (audit.actor, audit.action, audit.task_id) == (
        control.actor,
        "policy_migrated",
        None,
    )
    assert json.loads(audit.detail_json) == {
        "migration": MIGRATIONS[profile],
        "version": 150,
        "before": {key: before.get(key) for key in AUDIT_KEYS[profile]},
        "after": {key: after.get(key) for key in AUDIT_KEYS[profile]},
    }
    _run(session, profile)
    assert _control(session) == control
    assert _audits(session) == current
    for receipt, original in zip(receipts, pinned):
        session.refresh(receipt)
        assert receipt.model_dump() == original


GUARDS = [
    (
        "no_astra",
        {
            "allowed_models": ["luna", "opus", "sol"],
            "conductor_model": "opus",
            "worker_model": "luna",
            "model_pools": {"conductor": ["opus"], "worker": ["luna", "sol"]},
        },
    ),
    (
        "claude_55",
        {
            "allowed_models": ["opus", "sol", "sonnet", "spark"],
            "task_budget_usd": 60,
            "model_pools": {"worker": ["spark", "sol"]},
        },
    ),
    ("gen14_sol", {"generation": 15}),
    *[
        ("restore_astra", override)
        for override in (
            {"generation": 15},
            {"allowed_models": ["astra", "opus", "sol", "sonnet", "spark"]},
            {
                "conductor_model": "sol",
                "model_pools": {**SOL_POOLS, "conductor": ["sol"]},
            },
            {"repo": "another/repository"},
            {
                "allowed_models": [
                    "sol",
                    "spark",
                    "sonnet",
                    "opus",
                    "luna",
                    "terra",
                    "fable",
                    "pi-spark",
                ],
                "model_pools": {
                    "worker": [
                        "sol",
                        "spark",
                        "sonnet",
                        "opus",
                        "luna",
                        "terra",
                        "fable",
                        "pi-spark",
                    ]
                },
            },
            {
                "allowed_models": ["astra", "opus", "sol", "sonnet", "spark"],
                "model_pools": PLANNER_POOLS,
            },
        )
    ],
    *[
        ("astra_planner", override)
        for override in (
            {"generation": 15},
            {"conductor_model": "sol"},
            {"worker_model": "spark"},
            {"reviewer_model": "sol"},
            {"repo": "another/repository"},
            {"allowed_models": ["astra", "sol", "sonnet", "spark"]},
            {"allowed_models": ["astra", "opus", "sonnet", "spark"]},
            {"model_pools": {**PLANNER_POOLS, "conductor": ["opus", "astra", "sol"]}},
            {"model_pools": None},
            {"model_pools": {"worker": ["sol"] * 8}},
            {
                "allowed_models": ["astra", "opus", "sol"]
                + [f"other-{i}" for i in range(13)]
            },
        )
    ],
    *[
        ("coding_effort", override)
        for override in (
            {"generation": 15},
            {"worker_model": "spark"},
            {"conductor_model": "opus"},
            {"reviewer_model": "astra"},
        )
    ],
]


@pytest.mark.parametrize("profile,overrides", GUARDS)
def test_operator_changes_and_size_guards_are_untouched(session, profile, overrides):
    _seed(session, {**_policy(profile), **overrides})
    before, audits = _control(session), _audits(session)
    _run(session, profile)
    assert _control(session) == before
    assert _audits(session) == audits


@pytest.mark.parametrize("profile", ["restore_astra", "astra_planner", "coding_effort"])
def test_routing_and_quota_fallback_use_applied_rows(session, monkeypatch, profile):
    monkeypatch.delenv("SWARM_QUOTA_FLOORS", raising=False)
    _seed(session, _policy(profile))
    _run(session, profile)
    policy = json.loads(_control(session).policy_json)
    for role, expected in (
        ("conductor", "astra"),
        ("refine", "astra"),
        ("worker", "astra" if profile == "coding_effort" else "sol"),
        ("implement", "astra" if profile == "coding_effort" else "sol"),
        ("reviewer", "opus"),
    ):
        assert select_model(role, policy, quota={})["model"] == expected
    walled = {"codex": {"observed": True, "exhausted": True, "age_seconds": 0}}
    conductor = select_model("conductor", policy, quota=walled)
    assert conductor["model"] == "opus"
    assert conductor["fallback_from"] == "astra"
    refine = select_model("refine", policy, quota=walled)
    assert refine["model"] == "spark"
    assert [skip["model"] for skip in refine["skipped"]] == ["astra", "sol"]
    if profile == "coding_effort":
        for node, explicit, expected in (
            ("implement_work", None, "medium"),
            ("implement_migration", "xhigh", "xhigh"),
            ("correct_1", None, "xhigh"),
            ("review_1", None, "xhigh"),
        ):
            assert effort_for(node, policy, explicit) == expected


@pytest.mark.parametrize("profile", ["claude_55", "gen14_sol"])
def test_optional_pools_remain_absent(session, profile):
    policy = _policy(profile)
    del policy["model_pools"]
    _seed(session, policy)
    _run(session, profile)
    after = json.loads(_control(session).policy_json)
    assert "model_pools" not in after
    assert validate_policy(after) == after
    if profile == "claude_55":
        assert after["task_budget_usd"] == 50
        assert "sonnet" in after["allowed_models"]
    else:
        assert (after["worker_model"], after["generation"]) == ("sol", 14)


@pytest.mark.parametrize(
    "profile,has_spark",
    [("restore_astra", True), ("astra_planner", True), ("astra_planner", False)],
)
def test_missing_pools_keep_fallbacks_and_judge(session, profile, has_spark):
    before = _policy(profile)
    before["model_pools"] = {"worker": ["sol", "sonnet"], "judge": ["opus", "sol"]}
    if not has_spark:
        before["allowed_models"].remove("spark")
    _seed(session, before)
    _run(session, profile)
    after = json.loads(_control(session).policy_json)
    assert validate_policy(after)["model_pools"] == after["model_pools"]
    assert after["model_pools"] == {
        "conductor": ["astra", "opus", "sol"],
        "worker": ["sol", "sonnet", "astra"],
        "implement": ["sol", "sonnet", "astra"],
        "reviewer": ["opus", "astra"],
        "refine": ["astra", "spark", "sol"] if has_spark else ["astra", "sol"],
        "judge": ["opus", "sol"],
    }
    assert after["allowed_models"] == sorted(set(before["allowed_models"]) | {"astra"})
    if has_spark:
        walled = {"codex": {"observed": True, "exhausted": True, "age_seconds": 0}}
        assert select_model("refine", after, quota=walled)["model"] == "spark"


@pytest.mark.parametrize("profile", ["claude_55", "gen14_sol"])
def test_unrelated_pool_members_and_larger_budget_are_kept(session, profile):
    before = _policy(profile)
    if profile == "claude_55":
        before.update(
            task_budget_usd=80,
            worker_model="sol",
            model_pools={"worker": ["sol", "luna"], "conductor": ["opus"]},
        )
    else:
        before["model_pools"]["refine"] = ["spark"]
    _seed(session, before)
    _run(session, profile)
    after = json.loads(_control(session).policy_json)
    assert validate_policy(after) == after
    if profile == "claude_55":
        assert after["task_budget_usd"] == 80
        assert after["model_pools"] == before["model_pools"]
        assert "sonnet" in after["allowed_models"]
    else:
        assert after["model_pools"]["refine"] == ["spark"]
        assert after["model_pools"]["worker"] == ["sol", "spark", "sonnet"]


def test_astra_allowance_alone_triggers_removal(session):
    before = _policy("no_astra")
    before.update(
        conductor_model="opus",
        model_pools={"conductor": ["opus", "astra"], "worker": ["sol"]},
    )
    _seed(session, before)
    _run(session, "no_astra")
    after = json.loads(_control(session).policy_json)
    assert after == _expected("no_astra", before)
    assert validate_policy(after) == after


def test_existing_astra_members_keep_order_without_duplicates(session):
    before = _policy("astra_planner")
    before.update(
        allowed_models=["spark", "sol", "astra", "opus", "astra", "sonnet"],
        model_pools={
            **PLANNER_POOLS,
            "refine": ["sol", "astra", "spark", "sol"],
            "worker": ["sol", "astra", "spark", "sonnet"],
            "implement": ["sol", "spark", "astra", "sonnet"],
            "judge": ["sol", "opus"],
        },
    )
    _seed(session, before)
    _run(session, "astra_planner")
    after = json.loads(_control(session).policy_json)
    assert validate_policy(after)["model_pools"] == after["model_pools"]
    assert after == {
        **before,
        "allowed_models": ["astra", "opus", "sol", "sonnet", "spark"],
        "conductor_model": "astra",
        "model_pools": {
            **before["model_pools"],
            "conductor": ["astra", "opus", "sol"],
            "refine": ["astra", "sol", "spark"],
        },
    }


def test_followup_is_inert_after_successful_restoration(session):
    _seed(session, _policy("restore_astra"))
    _run(session, "restore_astra")
    before, audits = _control(session), _audits(session)
    assert json.loads(before.policy_json)["conductor_model"] == "astra"
    assert before.actor == "migration:" + MIGRATIONS["restore_astra"]
    _run(session, "astra_planner")
    assert _control(session) == before
    assert _audits(session) == audits
