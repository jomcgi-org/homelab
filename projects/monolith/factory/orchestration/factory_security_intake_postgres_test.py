"""Execute the guarded security intake migration against real PostgreSQL."""

from __future__ import annotations

import copy
import json

import pytest
from shared.testing.plugin import _find_migrations_dir
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from factory.orchestration.factory_models import FactoryReceipt

MIGRATION = "20261028120000_factory_security_intake"
ACTOR = f"migration:{MIGRATION}"
LEGACY = ["needs-human", "wontfix", "security-finding"]


def _policy():
    return {
        "repo": "jomcgi-org/homelab",
        "generation": 14,
        "issue_numbers": [6288],
        "auto_merge": False,
        "model_pools": {"conductor": ["astra", "opus"], "worker": ["sol"]},
        "max_tasks": {"delivery": 10, "advisory": 2},
        "task_budget_usd": 50,
        "intake": {
            "enabled": False,
            "refine_enabled": False,
            "close_enabled": False,
            "labels": ["agent-ready"],
            "exclude_labels": LEGACY,
            "max_per_day": 50,
        },
        "repos": {
            "jomcgi-org/homelab": {"exclude_labels": LEGACY},
            "other/repo": {"enabled": False, "exclude_labels": ["operator-only"]},
        },
    }


def _seed(session, policy):
    session.execute(
        text(
            "UPDATE swarm.factory_control SET policy_json = :policy, "
            "version = 149, actor = 'operator', state = 'paused', "
            "updated_at = '2026-10-01T00:00:00Z' WHERE id = 'factory'"
        ),
        {"policy": json.dumps(policy)},
    )


def _control(session):
    return (
        session.execute(
            text("SELECT * FROM swarm.factory_control WHERE id = 'factory'")
        )
        .one()
        ._mapping
    )


def _audits(session):
    return session.execute(text("SELECT * FROM swarm.factory_audit ORDER BY id")).all()


def _run(session):
    session.execute(text((_find_migrations_dir() / f"{MIGRATION}.sql").read_text()))


@pytest.mark.parametrize(
    "labels", [LEGACY, ["needs-human", "security-finding", "wontfix"]]
)
def test_legacy_global_lists_change_with_matching_atomic_audit(session, labels):
    policy = _policy()
    policy["intake"]["exclude_labels"] = labels
    _seed(session, policy)
    session.add(
        FactoryReceipt(
            repo="jomcgi-org/homelab",
            issue_number=7372,
            generation=14,
            title="Pinned authority",
            body="Unchanged",
            url="https://example.test/7372",
            actor="operator",
            state="escalated",
            policy_json=json.dumps(policy),
            allowance_json='{"turns":9,"budget_usd":50}',
            escalation_json='{"reason":"held"}',
        )
    )
    session.flush()
    before = dict(_control(session))
    audits_before = _audits(session)
    # Pinned task/receipt rows are outside the migration's write set.
    receipts_before = session.execute(text("SELECT * FROM swarm.factory_receipt")).all()
    _run(session)
    after = dict(_control(session))
    expected = copy.deepcopy(policy)
    expected["intake"]["exclude_labels"] = [
        v for v in labels if v != "security-finding"
    ]
    assert json.loads(after["policy_json"]) == expected
    assert after["version"] == before["version"] + 1
    assert after["actor"] == ACTOR
    assert after["updated_at"] > before["updated_at"]
    changed = {"policy_json", "version", "actor", "updated_at"}
    assert {k: v for k, v in after.items() if k not in changed} == {
        k: v for k, v in before.items() if k not in changed
    }
    audits = _audits(session)
    assert audits[:-1] == audits_before
    audit = audits[-1]._mapping
    assert (audit["actor"], audit["action"], audit["task_id"]) == (
        ACTOR,
        "policy_migrated",
        None,
    )
    assert json.loads(audit["detail_json"]) == {
        "migration": MIGRATION,
        "version": after["version"],
        "before": {"intake": policy["intake"]},
        "after": {"intake": expected["intake"]},
    }
    assert (
        session.execute(text("SELECT * FROM swarm.factory_receipt")).all()
        == receipts_before
    )
    _run(session)
    assert dict(_control(session)) == after
    assert _audits(session) == audits


@pytest.mark.parametrize(
    "labels",
    [
        None,
        "security-finding",
        {},
        1,
        True,
        [],
        ["needs-human", "wontfix"],
        ["needs-human", "wontfix", "operator-only"],
        ["needs-human", "security-finding", "wontfix", "operator-only"],
        ["security-finding"],
        LEGACY + ["security-finding"],
        ["wontfix", "needs-human", "security-finding"],
    ],
)
def test_custom_and_incompatible_exclusion_lists_are_noops(session, labels):
    policy = _policy()
    policy["intake"]["exclude_labels"] = labels
    _assert_noop(session, policy)


@pytest.mark.parametrize("intake", [None, [], "legacy", 1, True, {}])
def test_missing_and_incompatible_intake_blocks_are_noops(session, intake):
    policy = _policy()
    policy["intake"] = intake
    _assert_noop(session, policy)


@pytest.mark.parametrize(
    "policy", [{}, {"repo": "jomcgi-org/homelab"}, None, [], "legacy", 1]
)
def test_missing_intake_and_incompatible_policy_are_noops(session, policy):
    _assert_noop(session, policy)


def _assert_noop(session, policy):
    _seed(session, policy)
    before = dict(_control(session))
    audits_before = _audits(session)
    _run(session)
    assert dict(_control(session)) == before
    assert _audits(session) == audits_before


def test_other_repository_is_untouched(session):
    policy = _policy()
    policy["repo"] = "other/repo"
    _assert_noop(session, policy)


@pytest.mark.parametrize("missing", [False, True])
def test_wrong_or_missing_singleton_is_untouched(session, missing):
    _seed(session, _policy())
    if missing:
        session.execute(text("DELETE FROM swarm.factory_control WHERE id = 'factory'"))
    else:
        # Represent a legacy incompatible row without relaxing production DDL.
        # The shared harness rolls this test transaction back, including DDL.
        session.execute(
            text(
                "ALTER TABLE swarm.factory_control DROP CONSTRAINT factory_control_id_check"
            )
        )
        session.execute(
            text("UPDATE swarm.factory_control SET id = 'other' WHERE id = 'factory'")
        )
    before = session.execute(
        text("SELECT * FROM swarm.factory_control ORDER BY id")
    ).all()
    audits_before = _audits(session)
    _run(session)
    assert (
        session.execute(text("SELECT * FROM swarm.factory_control ORDER BY id")).all()
        == before
    )
    assert _audits(session) == audits_before


def test_audit_failure_rolls_back_policy_and_metadata(session):
    _seed(session, _policy())
    before = dict(_control(session))
    audits_before = _audits(session)
    session.execute(
        text("""
        CREATE FUNCTION swarm.reject_security_intake_audit() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.actor = 'migration:20261028120000_factory_security_intake' THEN
                RAISE EXCEPTION 'test audit unavailable';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER reject_security_intake_audit BEFORE INSERT ON swarm.factory_audit
        FOR EACH ROW EXECUTE FUNCTION swarm.reject_security_intake_audit();
    """)
    )
    with (
        pytest.raises(DBAPIError, match="test audit unavailable"),
        session.begin_nested(),
    ):
        _run(session)
    assert dict(_control(session)) == before
    assert _audits(session) == audits_before
