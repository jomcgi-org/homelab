"""Real PostgreSQL serialization of exact recurrence ownership and rolling quota."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine, select

from factory.orchestration import operator_readmission as readmission
from factory.orchestration.factory_intake import admit_next
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt
from factory.orchestration.operator_readmission_test import (
    setup,
    seed,
    preview,
    apply,
    OPERATOR,
)


@pytest.fixture
def engine(pg, monkeypatch):
    # Isolate committed concurrent transactions while using the established
    # real-Postgres harness. No production connection or live control is used.
    schema = "readmission_" + uuid4().hex
    base = create_engine(pg.url)
    with base.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = base.execution_options(schema_translate_map={"swarm": schema})
    setup(engine, monkeypatch)
    yield engine
    with base.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    base.dispose()


def race(calls):
    ready = Barrier(len(calls))

    def invoke(call):
        ready.wait(timeout=10)
        try:
            return call()
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}

    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        return list(executor.map(invoke, calls))


def test_duplicate_work_and_same_key_are_serialized(engine):
    seed(engine)
    p = preview()
    results = race([lambda: apply(p), lambda: apply(p)])
    assert results[0] == results[1] and results[0]["ok"]
    with Session(engine) as db:
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(
                        FactoryAudit.action == readmission.ACTION
                    )
                ).all()
            )
            == 1
        )
    assert admit_next("test")["receipt_id"] == results[0]["receipt_id"]


def test_different_keys_cannot_create_concurrent_recurrences(engine):
    seed(engine)
    first = preview(key="first")
    second = preview(key="second")
    results = race(
        [lambda: apply(first, key="first"), lambda: apply(second, key="second")]
    )
    assert sum(result["ok"] for result in results) == 1
    assert (
        next(result["reason"] for result in results if not result["ok"])
        == "existing_work_owner"
    )
    with Session(engine) as db:
        assert (
            len(
                db.exec(
                    select(FactoryReceipt).where(FactoryReceipt.state == "queued")
                ).all()
            )
            == 1
        )


def test_quota_race_is_shared_across_operators_and_recurrences(engine):
    for number in range(23):
        seed(engine, 8000 + number)
        apply(preview(8000 + number, str(number)), 8000 + number, str(number))
    other = replace(OPERATOR, subject="operator:other")
    seed(engine, 9001)
    seed(engine, 9002)
    first = preview(9001, "first")
    second = preview(9002, "second", principal=other)
    results = race(
        [
            lambda: apply(first, 9001, "first"),
            lambda: apply(second, 9002, "second", principal=other),
        ]
    )
    assert sum(result["ok"] for result in results) == 1
    assert (
        next(result["reason"] for result in results if not result["ok"])
        == "readmission_rolling_limit"
    )
    with Session(engine) as db:
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(
                        FactoryAudit.action == readmission.ACTION
                    )
                ).all()
            )
            == 24
        )
