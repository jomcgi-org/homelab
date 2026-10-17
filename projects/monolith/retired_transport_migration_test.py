"""Exercise retirement against runtime-created tables and shared dependencies."""

import pytest
from sqlalchemy.exc import DBAPIError
from sqlmodel import create_engine, text

from shared.testing.plugin import _find_migrations_dir


@pytest.fixture
def retirement_db(pg):
    engine = create_engine(pg.url)
    try:
        with engine.connect() as conn:
            with conn.begin():
                yield conn
                # Every test restores the fully migrated session-scoped database.
                conn.rollback()
    finally:
        engine.dispose()


def _retire(conn):
    sql = (_find_migrations_dir() / "20261017072500_retire_whatsapp.sql").read_text()
    conn.execute(text(sql))


def _restore_historical_transport(conn):
    for path in sorted(_find_migrations_dir().glob("20260708*whatsapp*.sql")):
        conn.execute(text(path.read_text()))


def _assert_retired(conn):
    assert conn.exec_driver_sql("SELECT to_regnamespace('whatsapp')").scalar() is None
    for name in ("outbox", "group", "pending_action", "reminder", "calendar_draft"):
        assert (
            conn.exec_driver_sql(f"SELECT to_regclass('chat.whatsapp_{name}')").scalar()
            is None
        )
    for name in (
        "chat.goosecracker_sessions",
        "chat.goosecracker_steering",
        "dr_jobs.nhs_vacancies",
    ):
        assert conn.exec_driver_sql(f"SELECT to_regclass('{name}')").scalar() == name
    assert conn.exec_driver_sql(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = 'dr_jobs' AND table_name = 'nhs_vacancies' "
        "AND column_name = 'notified_whatsapp'"
    ).scalar() == 1


@pytest.mark.parametrize("runtime_tables", [False, True])
def test_retire_historical_transport_with_empty_or_populated_schema(
    retirement_db, runtime_tables
):
    conn = retirement_db
    _restore_historical_transport(conn)
    if runtime_tables:
        conn.exec_driver_sql(
            "CREATE TABLE whatsapp.whatsmeow_device (id integer PRIMARY KEY);"
            "CREATE TABLE whatsapp.whatsmeow_sessions "
            "(device_id integer REFERENCES whatsapp.whatsmeow_device(id));"
            "INSERT INTO whatsapp.whatsmeow_device VALUES (1);"
            "INSERT INTO whatsapp.whatsmeow_sessions VALUES (1);"
        )
    _retire(conn)
    _assert_retired(conn)
    _retire(conn)
    _assert_retired(conn)


def test_full_migration_chain_retires_transport_and_can_repeat(retirement_db):
    _assert_retired(retirement_db)
    _retire(retirement_db)
    _assert_retired(retirement_db)


@pytest.mark.parametrize(
    "unexpected_object",
    [
        "CREATE TABLE whatsapp.unowned (id integer)",
        "CREATE TABLE whatsapp.whatsmeowXunowned (id integer)",
        "CREATE VIEW chat.shared_transport_view AS "
        "SELECT id FROM whatsapp.whatsmeow_device",
        "CREATE VIEW chat.shared_transport_view AS SELECT id FROM chat.whatsapp_outbox",
    ],
)
def test_retirement_refuses_unexpected_objects_and_shared_dependencies(
    retirement_db, unexpected_object
):
    conn = retirement_db
    _restore_historical_transport(conn)
    conn.exec_driver_sql("CREATE TABLE whatsapp.whatsmeow_device (id integer)")
    conn.exec_driver_sql(unexpected_object)
    with pytest.raises(DBAPIError, match="depend"):
        with conn.begin_nested():
            _retire(conn)
    # Failure rolls back the retirement, including its earlier table drops.
    assert conn.exec_driver_sql(
        "SELECT to_regclass('chat.whatsapp_outbox')"
    ).scalar() == "chat.whatsapp_outbox"
    assert conn.exec_driver_sql(
        "SELECT to_regclass('whatsapp.whatsmeow_device')"
    ).scalar() == "whatsapp.whatsmeow_device"
