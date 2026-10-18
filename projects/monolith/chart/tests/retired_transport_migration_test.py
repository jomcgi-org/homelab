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
    # pg has already applied the later feature retirement. Restore the actual
    # shared prerequisites of the historical WhatsApp ALTER TABLE statements.
    for filename in (
        "20260629120000_goosecracker_sessions.sql",
        "20260703030000_chat_goosecracker_steering.sql",
    ):
        conn.execute(text((_find_migrations_dir() / filename).read_text()))
    for path in sorted(_find_migrations_dir().glob("20260708*whatsapp*.sql")):
        conn.execute(text(path.read_text()))


def _assert_retired(conn, *, whatsapp_column=False, historical_transport=False):
    assert conn.exec_driver_sql("SELECT to_regnamespace('whatsapp')").scalar() is None
    for name in ("outbox", "group", "pending_action", "reminder", "calendar_draft"):
        assert (
            conn.exec_driver_sql(f"SELECT to_regclass('chat.whatsapp_{name}')").scalar()
            is None
        )
    for name in (
        "chat.goosecracker_sessions",
        "chat.goosecracker_steering",
    ):
        assert conn.exec_driver_sql(f"SELECT to_regclass('{name}')").scalar() == (
            name if historical_transport else None
        )
    assert (
        conn.exec_driver_sql("SELECT to_regclass('dr_jobs.nhs_vacancies')").scalar()
        == "dr_jobs.nhs_vacancies"
    )
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
    _assert_retired(conn, whatsapp_column=True, historical_transport=True)
    _retire(conn)
    _assert_retired(conn, whatsapp_column=True, historical_transport=True)


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


_FEATURE_RETIREMENT = "20261018123000_retire_unused_feature_schema.sql"
_RETIRED_TABLES = (
    "public.demo_pg_savings",
    "public.demo_sg_savings",
    "public.bazel_query_savings",
    "public.ember_k8s_wake_event",
    "home.archives",
    "knowledge.ingest_queue",
    "observability.topology_snapshot",
    "chat.goosecracker_steering",
    "chat.goosecracker_sessions",
    "claude_agent.agent_base_snapshots",
    "claude_agent.agent_threads",
    "demo.load_run",
    "demo.load_scan",
    "semgrep.scan_perf",
)
_RETIRED_VIEWS = (
    "public_api.agent_activity_daily",
    "public_api.agent_activity_now",
    "public_api.local_session_activity_daily",
)
_ACTIVE_TABLES = (
    "home.tasks",
    "knowledge.raw_inputs",
    "chat.discord_outbox",
    "claude_agent.routine_jobs",
    "agent_sessions.agent_sessions",
    "agent_sessions.agent_turns",
    "dr_jobs.nhs_vacancies",
    "observability.stats_snapshot",
    "observability.merged_prs",
    "observability.factory_goals",
    "observability.factory_goal_issues",
    "public_api.agent_activity_snapshot",
    "public_api.factory_activity_snapshot",
)


@pytest.fixture(scope="module")
def pre_feature_engine(pg):
    # pg already contains the full chain. Build a separate database through the
    # actual historical files so upgrades exercise every ALTER, grant and FK.
    admin = create_engine(pg.url, isolation_level="AUTOCOMMIT")
    engine = create_engine(admin.url.set(database="retired_features_upgrade"))
    with admin.connect() as conn:
        conn.exec_driver_sql("CREATE DATABASE retired_features_upgrade")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE EXTENSION vector")
            for path in sorted(_find_migrations_dir().glob("*.sql")):
                if path.name >= _FEATURE_RETIREMENT:
                    break
                conn.execute(text(path.read_text()))
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql("DROP DATABASE retired_features_upgrade")
        admin.dispose()


@pytest.fixture
def pre_feature_db(pre_feature_engine):
    with pre_feature_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


def _retire_features(conn):
    conn.execute(text((_find_migrations_dir() / _FEATURE_RETIREMENT).read_text()))


def _assert_features_retired(conn):
    for name in (*_RETIRED_TABLES, *_RETIRED_VIEWS):
        assert (
            conn.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar()
            is None
        )
    for name in ("demo", "semgrep"):
        assert (
            conn.execute(text("SELECT to_regnamespace(:name)"), {"name": name}).scalar()
            is None
        )
    assert (
        conn.exec_driver_sql(
            "SELECT to_regprocedure('claude_agent.notify_thread_pending()')"
        ).scalar()
        is None
    )
    assert (
        conn.exec_driver_sql(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'agent_threads_pending_notify'"
        ).scalar()
        == 0
    )
    for name in (
        "ingest_queue_status",
        "ingest_queue_created_at",
        "ingest_queue_pkey",
        "ingest_queue_id_seq",
    ):
        assert (
            conn.execute(
                text("SELECT to_regclass(:name)"), {"name": f"knowledge.{name}"}
            ).scalar()
            is None
        )
    for name in _ACTIVE_TABLES:
        assert (
            conn.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar()
            is not None
        )


def _seed_feature_upgrade(conn):
    conn.exec_driver_sql("""
        INSERT INTO public.demo_pg_savings (total_mib_seconds) VALUES (123);
        INSERT INTO public.demo_sg_savings (scans, actual_ms, saved_ms) VALUES (3, 4, 5);
        INSERT INTO public.bazel_query_savings (total_analysis_s_saved) VALUES (6);
        INSERT INTO public.ember_k8s_wake_event (from_state, duration_ms, classification)
            VALUES ('banked', 7, 'wake');
        INSERT INTO home.archives (date, content) VALUES (CURRENT_DATE, 'retired');
        INSERT INTO knowledge.ingest_queue (url, source_type)
            VALUES ('https://example.invalid/retired', 'webpage');
        INSERT INTO observability.topology_snapshot (payload) VALUES ('{"retired":true}');
        INSERT INTO chat.goosecracker_sessions (discord_thread) VALUES ('retired');
        INSERT INTO chat.goosecracker_steering (thread_id, message_id, author_id, text)
            VALUES ('retired', 'message', 'author', 'retired');
        INSERT INTO claude_agent.agent_base_snapshots (base_ref, repo, arch)
            VALUES ('retired-base', 'retired-repo', 'amd64');
        INSERT INTO claude_agent.agent_threads (thread_id, base_snapshot_ref)
            VALUES ('retired-thread', 'retired-base');
        INSERT INTO demo.load_run (id, workload, config)
            VALUES ('00000000-0000-0000-0000-000000000001', 'retired', '{}');
        INSERT INTO demo.load_scan (run_id, workload, seq, name, status, latency_ms)
            VALUES ('00000000-0000-0000-0000-000000000001', 'retired', 1, 'scan', 'done', 8);
        INSERT INTO semgrep.scan_perf (scan_id, file_count, changed_lines)
            VALUES (1, 2, 3);
        INSERT INTO home.tasks (task) VALUES ('keep active task');
        INSERT INTO observability.stats_snapshot (payload) VALUES ('{"keep":"stats"}');
        INSERT INTO public_api.agent_activity_snapshot (payload) VALUES ('{"keep":"activity"}');
        INSERT INTO knowledge.raw_inputs (raw_id, path, source, content_hash)
            VALUES ('keep-raw', 'keep.md', 'capture', 'keep-hash');
    """)
    for name in _RETIRED_TABLES:
        assert conn.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar() == 1


def _active_contract(conn):
    # Row values, relation identities, ACLs and constraints must be unchanged.
    return {
        name: (
            conn.exec_driver_sql(
                f"SELECT to_jsonb(t) FROM {name} t ORDER BY to_jsonb(t)::text"
            ).all(),
            conn.execute(
                text("SELECT oid, relacl FROM pg_class WHERE oid = to_regclass(:name)"),
                {"name": name},
            ).one(),
            conn.execute(
                text(
                    "SELECT oid, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = to_regclass(:name) ORDER BY oid"
                ),
                {"name": name},
            ).all(),
        )
        for name in _ACTIVE_TABLES
    }


def test_full_chain_retires_features_and_repeats(retirement_db):
    _assert_features_retired(retirement_db)
    _retire_features(retirement_db)
    _assert_features_retired(retirement_db)


def test_populated_feature_upgrade_preserves_active_rows_and_grants(pre_feature_db):
    conn = pre_feature_db
    _seed_feature_upgrade(conn)
    before = _active_contract(conn)
    assert (
        conn.exec_driver_sql(
            "SELECT to_regprocedure('claude_agent.notify_thread_pending()')"
        ).scalar()
        is not None
    )
    assert (
        conn.exec_driver_sql(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'agent_threads_pending_notify'"
        ).scalar()
        == 1
    )
    # All drops are qualified even if another schema is first on the search path.
    conn.exec_driver_sql(
        "CREATE TABLE home.demo_pg_savings (id integer); INSERT INTO home.demo_pg_savings VALUES (42)"
    )
    conn.exec_driver_sql("SET LOCAL search_path = home, public")
    _retire_features(conn)
    _assert_features_retired(conn)
    assert _active_contract(conn) == before
    assert conn.exec_driver_sql("SELECT id FROM home.demo_pg_savings").scalar() == 42
    _retire_features(conn)
    _assert_features_retired(conn)
    assert _active_contract(conn) == before
    conn.exec_driver_sql("SET LOCAL ROLE public_reader")
    assert conn.exec_driver_sql(
        "SELECT payload FROM observability.stats_snapshot"
    ).scalar() == {"keep": "stats"}
    assert conn.exec_driver_sql(
        "SELECT payload FROM public_api.agent_activity_snapshot"
    ).scalar() == {"keep": "activity"}


@pytest.mark.parametrize(
    "missing",
    [
        "DROP TABLE public.demo_pg_savings; DROP VIEW public_api.agent_activity_now",
        "DROP TABLE demo.load_scan; DROP TABLE demo.load_run; DROP SCHEMA demo RESTRICT",
        "DROP TABLE semgrep.scan_perf; DROP SCHEMA semgrep RESTRICT",
        "DROP TRIGGER agent_threads_pending_notify ON claude_agent.agent_threads",
        "DROP TRIGGER agent_threads_pending_notify ON claude_agent.agent_threads; "
        "DROP FUNCTION claude_agent.notify_thread_pending()",
    ],
)
def test_feature_retirement_tolerates_missing_objects(pre_feature_db, missing):
    conn = pre_feature_db
    conn.exec_driver_sql(missing)
    _retire_features(conn)
    _assert_features_retired(conn)


@pytest.mark.parametrize(
    "dependency",
    [
        *(
            f"CREATE VIEW home.shared_feature_dependency AS SELECT count(*) FROM {name}"
            for name in (*_RETIRED_TABLES, *_RETIRED_VIEWS)
        ),
        "CREATE TABLE home.shared_feature_dependency (run_id uuid REFERENCES demo.load_run(id))",
        "CREATE TABLE home.shared_feature_dependency (scan_id bigint REFERENCES semgrep.scan_perf(id))",
        "CREATE TABLE home.shared_feature_dependency (id bigint DEFAULT nextval('demo.load_scan_id_seq'))",
        "CREATE TRIGGER shared_feature_dependency AFTER INSERT ON home.tasks "
        "FOR EACH ROW EXECUTE FUNCTION claude_agent.notify_thread_pending()",
        "CREATE TABLE demo.unexpected (id integer); "
        "CREATE VIEW home.shared_feature_dependency AS SELECT * FROM demo.unexpected",
        "CREATE FUNCTION semgrep.unexpected() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
        "CREATE TYPE demo.unexpected AS ENUM ('keep')",
    ],
)
def test_feature_retirement_refuses_dependencies_atomically(pre_feature_db, dependency):
    conn = pre_feature_db
    _seed_feature_upgrade(conn)
    conn.exec_driver_sql(dependency)
    before = _active_contract(conn)
    # Capture every schema object, including the unexpected dependency, its
    # constraints and grants. Refusal must restore even earlier successful drops.
    catalog = "SELECT classid, objid, objsubid, refclassid, refobjid, refobjsubid, deptype FROM pg_depend ORDER BY 1, 2, 3, 4, 5, 6, 7"
    dependencies = conn.exec_driver_sql(catalog).all()
    with pytest.raises(DBAPIError, match="depend") as error:
        with conn.begin_nested():
            _retire_features(conn)
    assert error.value.orig.sqlstate == "2BP01"
    assert conn.exec_driver_sql(catalog).all() == dependencies
    assert _active_contract(conn) == before
    for name in _RETIRED_TABLES:
        assert conn.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar() == 1
    for name in _RETIRED_VIEWS:
        assert (
            conn.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar()
            is not None
        )
    assert (
        conn.exec_driver_sql(
            "SELECT to_regprocedure('claude_agent.notify_thread_pending()')"
        ).scalar()
        is not None
    )
