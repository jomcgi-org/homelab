"""Real PostgreSQL migration, durable uniqueness and compare-and-swap coverage."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import create_engine

from factory.orchestration import tracking


def _create(identity, **overrides):
    values = {
        "request_key": "create",
        "title": "Private task",
        "tracking_kind": "manual",
        "owner": "Human owner",
        "source_ref": f"https://example.test/{identity}",
    }
    return tracking.Create(**{**values, **overrides})


def _parallel(*functions):
    barrier = Barrier(len(functions))

    def call(function):
        barrier.wait(timeout=10)
        try:
            return function()
        except tracking.TrackingError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        return list(pool.map(call, functions))


@pytest.mark.parametrize("kind", ["same_request", "conflicting_request", "same_source"])
def test_postgres_concurrent_create(pg, kind):
    engine = create_engine(pg.url)
    identity = uuid4().hex
    actor = f"tracking-test:{identity}"
    first = _create(identity)
    second = (
        first
        if kind == "same_request"
        else _create(
            identity,
            request_key="other" if kind == "same_source" else "create",
            title="Other title",
        )
    )
    try:
        results = _parallel(
            lambda: tracking.record(engine, actor, first),
            lambda: tracking.record(engine, actor, second),
        )
        if kind == "same_request":
            assert results[0] == results[1]
        else:
            assert sum(isinstance(result, dict) for result in results) == 1
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM swarm.work_item WHERE source_ref = :source"
                    ),
                    {"source": first.source_ref},
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM swarm.tracking_request WHERE actor = :actor"
                    ),
                    {"actor": actor},
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()


def test_postgres_cas_and_replay_after_intervening_update(pg):
    engine = create_engine(pg.url)
    identity = uuid4().hex
    actor = f"tracking-test:{identity}"
    body = _create(identity)
    try:
        first = tracking.record(engine, actor, body)
        item_id = first["item"]["id"]
        results = _parallel(
            *(
                lambda key=key: tracking.record(
                    engine,
                    actor,
                    tracking.Change(
                        request_key=key,
                        expected_version=1,
                        state="done",
                        completion_evidence="Owner confirmed",
                    ),
                    item_id=item_id,
                )
                for key in ("a", "b")
            )
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        assert "stale expected_version" in results
        assert tracking.record(engine, actor, body) == first
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM swarm.work_item_event WHERE work_item_id = :id"
                    ),
                    {"id": item_id},
                ).scalar_one()
                == 2
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE swarm.work_item SET execution_mode='factory' WHERE id=:id",
        "UPDATE swarm.work_item SET owner='replacement' WHERE id=:id",
        "UPDATE swarm.work_item SET source_ref='https://example.test/replacement' WHERE id=:id",
        "UPDATE swarm.work_item SET authority='github' WHERE id=:id",
        "UPDATE swarm.work_item SET github_repo='owner/repo', github_issue_number=7400 WHERE id=:id",
        "DELETE FROM swarm.work_item WHERE id=:id",
        (
            "INSERT INTO swarm.factory_receipt (repo, issue_number, generation, title, body, url, actor, work_item_id) "
            "VALUES ('owner/repo',7400,0,'claim','','https://github.com/owner/repo/issues/7400','forged',:id)"
        ),
    ],
)
def test_sql_blocks_conversion_mislinks_and_deletion(pg, statement):
    engine = create_engine(pg.url)
    identity = uuid4().hex
    try:
        item_id = tracking.record(engine, identity, _create(identity))["item"]["id"]
        if "execution_mode=" in statement:
            expected = "execution mode is immutable"
        elif statement.startswith("DELETE"):
            expected = "tracking records cannot be deleted"
        elif statement.startswith("INSERT"):
            expected = "receipt work item is non-executable or missing"
        elif "github_repo=" in statement:
            expected = "work_item_tracking_check"
        else:
            expected = "tracking identity is immutable"
        with pytest.raises(DBAPIError, match=expected), engine.begin() as connection:
            connection.execute(text(statement), {"id": item_id})
        assert tracking.read(engine, item_id)["item"]["tracking_version"] == 1
    finally:
        engine.dispose()


def test_migration_private_grants_and_backwards_default(pg):
    with create_engine(pg.url).connect() as connection:
        assert not connection.execute(
            text(
                "SELECT has_table_privilege('public_reader','swarm.tracking_request','SELECT')"
            )
        ).scalar_one()
        default = connection.execute(
            text(
                "SELECT column_default FROM information_schema.columns WHERE table_schema='swarm' "
                "AND table_name='work_item' AND column_name='execution_mode'"
            )
        ).scalar_one()
        assert default == "'factory'::text"
        columns = (
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='swarm' "
                    "AND table_name='work_item'"
                )
            )
            .scalars()
            .all()
        )
        assert {"execution_mode", "tracking_version", "owner", "completed_at"} <= set(
            columns
        )
