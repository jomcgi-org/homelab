import json
from types import SimpleNamespace

import pytest

from factory.execution import push_format


def row(key="implement_test"):
    return SimpleNamespace(
        id=10,
        node_key=key,
        node_attempt=1,
        repo="jomcgi-org/homelab",
        branch="factory/t-test",
        workflow_id="format-dispatch",
        local_session_id=f"factory:t-test:{key}:1",
    )


@pytest.fixture
def db(tmp_path):
    from sqlmodel import Session, SQLModel, create_engine
    from factory.orchestration.factory_models import FactoryAudit
    from factory.orchestration.models import SwarmTask, SwarmNodeRun

    engine = create_engine(
        f"sqlite:///{tmp_path / 'audit.db'}",
        execution_options={"schema_translate_map": {"swarm": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[SwarmTask.__table__, SwarmNodeRun.__table__, FactoryAudit.__table__],
    )
    with Session(engine) as session:
        session.add(SwarmTask(id="t-test", task_text="test", conductor_model="sol"))
        session.commit()
        yield session


def admit(db, source, branch="factory/t-test", *, bound=True):
    from factory.orchestration.models import SwarmNodeRun

    db.add(
        SwarmNodeRun(
            task_id="t-test",
            node_key=source.node_key,
            attempt=source.node_attempt,
            dispatch_key=source.workflow_id,
            session_id=source.id if bound else None,
            status="running",
            pin_json=json.dumps(
                {
                    "repo": source.repo,
                    "branch": branch,
                    "hydration_branch": source.branch,
                }
            ),
        )
    )
    db.commit()


@pytest.mark.parametrize("key", ["implement_test", "correct_1", "integrate_1"])
@pytest.mark.parametrize("bound", [False, True])
def test_source_roles_receive_exact_contract(key, db, bound):
    source = row(key)
    admit(db, source, bound=bound)
    assert push_format.contract(source, db) == {
        "task_id": "t-test",
        "node_key": key,
        "branch": "factory/t-test",
        "base_ref": "refs/remotes/origin/main",
    }


@pytest.mark.parametrize(
    "hydrate,push",
    [
        ("main", "factory/t-test"),
        ("factory/t-test", "factory/t-test-node-one"),
        ("fix/adopted", "fix/adopted"),
    ],
)
def test_contract_uses_pinned_push_branch(db, hydrate, push):
    source = row()
    source.branch = hydrate
    admit(db, source, push)
    assert push_format.contract(source, db)["branch"] == push


def test_missing_or_foreign_pin_cannot_assign_push_branch(db):
    source = row()
    with pytest.raises(ValueError, match="admitted"):
        push_format.contract(source, db)
    admit(db, source)
    source.id += 1
    with pytest.raises(ValueError, match="admitted"):
        push_format.contract(source, db)


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "other-task"),
        ("node_key", "implement_other"),
        ("attempt", 2),
        ("dispatch_key", "other-dispatch"),
        ("session_id", 999),
    ],
)
def test_contract_rejects_foreign_attempt_or_dispatch(db, bound, field, value):
    from sqlmodel import select
    from factory.orchestration.models import SwarmNodeRun

    source = row()
    admit(db, source, bound=bound)
    run = db.exec(select(SwarmNodeRun)).one()
    setattr(run, field, value)
    db.commit()
    with pytest.raises(ValueError, match="admitted"):
        push_format.contract(source, db)


def test_absent_evidence_does_not_read_contract():
    source = row()
    source.branch = "main"
    push_format.record(None, source, None)


@pytest.mark.parametrize(
    "key",
    [
        "conductor_plan",
        "review_1",
        "refine_1",
        "scout_collect_1",
        "implement_browser_capture_mobile",
        "correct_not_a_round",
    ],
)
def test_read_only_roles_do_not_gain_push_behavior(key):
    assert push_format.contract(row(key)) is None


def test_identity_mismatch_fails_closed():
    source = row()
    source.node_attempt = 2
    with pytest.raises(ValueError, match="identity"):
        push_format.contract(source)


def test_drift_audit_counts_distinct_paths_once_across_results(tmp_path):
    from sqlmodel import Session, SQLModel, create_engine, select
    from factory.orchestration.factory_models import FactoryAudit
    from factory.orchestration.models import SwarmTask, SwarmNodeRun

    engine = create_engine(
        f"sqlite:///{tmp_path / 'audit.db'}",
        execution_options={"schema_translate_map": {"swarm": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[SwarmTask.__table__, FactoryAudit.__table__, SwarmNodeRun.__table__],
    )
    with Session(engine) as db:
        db.add(SwarmTask(id="t-test", task_text="test", conductor_model="sol"))
        db.commit()
        source = row()
        admit(db, source)
        value = {
            **push_format.contract(source, db),
            "outcome": "drift_blocked",
            "paths": ["a.py", "b.py", "a.py"],
            "file_count": 2,
        }
        push_format.record(db, source, value)
        db.commit()
        push_format.record(db, source, value)
        db.commit()
        value["paths"] = ["a.py", "b.py", "c.py"]
        value["file_count"] = 3
        push_format.record(db, source, value)
        db.commit()
        audits = db.exec(select(FactoryAudit)).all()
        details = [json.loads(a.detail_json) for a in audits]
        assert len(audits) == 2
        assert sum(d["file_count"] for d in details) == 3
        assert all(a.action == "format_drift_prevented" for a in audits)
        assert all(d["node_key"] == source.node_key for d in details)
        push_format.record(db, source, None)
        db.commit()
        assert len(db.exec(select(FactoryAudit)).all()) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"task_id": "foreign-task"},
        {"outcome": "tool_error"},
        {"file_count": 7},
        {"file_count": True},
        {"paths": []},
        {"paths": "a.py"},
        {"paths": ["../a.py"]},
        {"paths": ["/a.py"]},
        {"paths": [{}]},
        {"paths": ["a.py"] * 10001},
        None,
    ],
)
def test_rejected_evidence_records_only_trusted_identity(db, change):
    from sqlmodel import select
    from factory.orchestration.factory_models import FactoryAudit

    source = row()
    admit(db, source)
    value = {
        **push_format.contract(source, db),
        "outcome": "drift_blocked",
        "paths": ["a.py"],
        "file_count": 1,
    }
    invalid = {**value, **change} if change is not None else []
    push_format.record(db, source, invalid)
    db.commit()
    audit = db.exec(select(FactoryAudit)).one()
    assert audit.action == "format_drift_evidence_rejected"
    assert audit.task_id == "t-test"
    detail = json.loads(audit.detail_json)
    assert detail == {
        "node_key": source.node_key,
        "session_id": source.id,
        "reason": (
            "push-format result identity mismatch"
            if change is None or "task_id" in change
            else "invalid prevented-drift result"
        ),
    }
