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


def _assert_retired(conn, *, whatsapp_column=False):
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
    ).scalar() == int(whatsapp_column)


@pytest.mark.parametrize("runtime_tables", [False, True])
def test_retire_historical_transport_with_empty_or_populated_schema(
    retirement_db, runtime_tables
):
    conn = retirement_db
    # Recreate the pre-column-cleanup state to test the historical retirement.
    conn.exec_driver_sql(
        "ALTER TABLE dr_jobs.nhs_vacancies ADD COLUMN notified_whatsapp timestamptz"
    )
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
    _assert_retired(conn, whatsapp_column=True)
    _retire(conn)
    _assert_retired(conn, whatsapp_column=True)


def test_full_migration_chain_retires_transport_and_can_repeat(retirement_db):
    _assert_retired(retirement_db)
    _retire(retirement_db)
    _assert_retired(retirement_db)


def _drop_vacancy_whatsapp_column(conn):
    sql = (
        _find_migrations_dir() / "20261017110000_dr_jobs_drop_notified_whatsapp.sql"
    ).read_text()
    conn.execute(text(sql))


def _restore_vacancy_whatsapp_column(conn):
    conn.exec_driver_sql(
        "ALTER TABLE dr_jobs.nhs_vacancies ADD COLUMN notified_whatsapp timestamptz"
    )
    conn.exec_driver_sql(
        "INSERT INTO dr_jobs.nhs_vacancies "
        "(job_id, reference, title, employment_type, salary_band, salary_text, "
        "town, postcode, region, posted_date, closing_date, url, first_seen_at, "
        "last_seen_at, scraped_at, notified_discord, notified_whatsapp) VALUES "
        "('cleanup-pending', 'PS1', 'Consultant', 'Permanent', 'Consultant', "
        "'band', 'Elgin', 'IV30 1SN', 'Moray', '2026-06-10', '2026-12-01', "
        "'https://example.invalid/1', '2026-06-10 01:00Z', '2026-10-17 02:00Z', "
        "'2026-10-17 03:00Z', NULL, '2026-07-13 04:00Z'), "
        "('cleanup-delivered', '', 'Locum', 'Temporary', '', '', '', '', '', "
        "NULL, NULL, 'https://example.invalid/2', '2026-06-11 01:00Z', "
        "'2026-10-16 02:00Z', '2026-10-16 03:00Z', '2026-07-14 05:00Z', NULL)"
    )


def _vacancy_rows(conn):
    # Compare every other value, including NULL/pending and delivered state.
    return (
        conn.exec_driver_sql(
            "SELECT to_jsonb(v) - 'notified_whatsapp' "
            "FROM dr_jobs.nhs_vacancies v ORDER BY job_id"
        )
        .scalars()
        .all()
    )


def _vacancy_columns(conn):
    return conn.exec_driver_sql(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'dr_jobs' "
        "AND table_name = 'nhs_vacancies' "
        "AND column_name <> 'notified_whatsapp' ORDER BY ordinal_position"
    ).all()


def test_vacancy_column_cleanup_preserves_rows_and_other_columns(retirement_db):
    conn = retirement_db
    _restore_vacancy_whatsapp_column(conn)
    rows, columns = _vacancy_rows(conn), _vacancy_columns(conn)
    assert {c[0] for c in columns if c[0].startswith("notified_")} == {
        "notified_discord"
    }
    _drop_vacancy_whatsapp_column(conn)
    _assert_retired(conn)
    assert _vacancy_rows(conn) == rows
    assert _vacancy_columns(conn) == columns


def test_vacancy_column_cleanup_refuses_shared_view_without_destruction(retirement_db):
    conn = retirement_db
    _restore_vacancy_whatsapp_column(conn)
    conn.exec_driver_sql(
        "CREATE VIEW dr_jobs.shared_notification_view AS "
        "SELECT job_id, notified_whatsapp FROM dr_jobs.nhs_vacancies"
    )
    rows, columns = _vacancy_rows(conn), _vacancy_columns(conn)
    view_rows = conn.exec_driver_sql(
        "SELECT * FROM dr_jobs.shared_notification_view ORDER BY job_id"
    ).all()
    with pytest.raises(DBAPIError, match="depend") as error:
        with conn.begin_nested():
            _drop_vacancy_whatsapp_column(conn)
    assert error.value.orig.sqlstate == "2BP01"  # dependent_objects_still_exist
    _assert_retired(conn, whatsapp_column=True)
    assert _vacancy_rows(conn) == rows
    assert _vacancy_columns(conn) == columns
    assert (
        conn.exec_driver_sql(
            "SELECT * FROM dr_jobs.shared_notification_view ORDER BY job_id"
        ).all()
        == view_rows
    )


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
    assert (
        conn.exec_driver_sql("SELECT to_regclass('chat.whatsapp_outbox')").scalar()
        == "chat.whatsapp_outbox"
    )
    assert (
        conn.exec_driver_sql("SELECT to_regclass('whatsapp.whatsmeow_device')").scalar()
        == "whatsapp.whatsmeow_device"
    )
