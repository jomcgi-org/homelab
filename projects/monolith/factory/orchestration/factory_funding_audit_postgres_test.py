"""Canonical funding authority and rollback projections on real PostgreSQL."""

import json

from sqlmodel import select

from factory.orchestration import factory_funding as funding
from factory.orchestration.factory_models import FactoryAudit, funding_audit_filter
from factory.orchestration.models import SwarmTask


def test_new_and_historical_authority_coexist_without_double_reading(session):
    task_id = "funding-audit-compatibility"
    session.add(
        SwarmTask(id=task_id, task_text="audit", conductor_model="astra", budget_usd=50)
    )
    session.flush()
    old = FactoryAudit(
        actor="operator",
        action=funding.CONTINUATION_LEASE,
        task_id=task_id,
        detail_json=json.dumps({"head_sha": "a" * 40}),
    )
    session.add(old)
    session.flush()
    assert (
        funding.latest(session, task_id, funding.CONTINUATION_LEASE)["audit_id"]
        == old.id
    )
    funding.record_audit(
        session,
        "operator",
        funding.CONTINUATION_LEASE,
        task_id=task_id,
        head_sha="b" * 40,
    )
    session.flush()
    authoritative = session.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == task_id,
            funding_audit_filter(funding.CONTINUATION_LEASE),
        )
        .order_by(FactoryAudit.id)
    ).all()
    assert [r.action for r in authoritative] == [
        funding.CONTINUATION_LEASE,
        "funding_continuation_lease",
    ]
    assert (
        funding.latest(session, task_id, funding.CONTINUATION_LEASE)["audit_id"]
        == authoritative[-1].id
    )
    legacy = session.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == task_id,
            FactoryAudit.action == funding.CONTINUATION_LEASE,
        )
        .order_by(FactoryAudit.id.desc())
    ).first()
    assert json.loads(legacy.detail_json)["head_sha"] == "b" * 40
    assert legacy.id > authoritative[-1].id
    # A rolling-back writer can append an old-format event. Both generations
    # select that newer event; neither reuses a superseded canonical lease.
    historical = FactoryAudit(
        actor="operator",
        action=funding.CONTINUATION_LEASE,
        task_id=task_id,
        detail_json=json.dumps({"head_sha": "c" * 40}),
    )
    session.add(historical)
    session.flush()
    assert (
        funding.latest(session, task_id, funding.CONTINUATION_LEASE)["audit_id"]
        == historical.id
    )
    assert funding.continuation_status(session, task_id)["head_sha"] == "c" * 40
