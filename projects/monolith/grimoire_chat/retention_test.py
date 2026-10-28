"""Real-Postgres tests for Grimoire chat retention and takedown (ADR security/005).

The `pg` fixture applies every migration and is shared across tests, so every
test uses unique ids and asserts only on its own rows. Hand-written bdd_test
(grimoire_chat is gazelle-excluded), registered in projects/monolith/BUILD.
"""

import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlmodel import Session, create_engine, text
from typer.testing import CliRunner

from grimoire_chat import retention

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


@pytest.fixture
def db(pg):
    engine = create_engine(pg.url)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _session(db, sid, *, idle_days=0, ip_hash=None, messages=0):
    db.execute(
        text(
            "INSERT INTO grimoire_chat.sessions (id, created_at, last_seen_at, ip_hash) "
            "VALUES (:id, :seen, :seen, :ip)"
        ),
        {"id": sid, "seen": NOW - timedelta(days=idle_days), "ip": ip_hash},
    )
    for i in range(messages):
        db.execute(
            text(
                "INSERT INTO grimoire_chat.messages (session_id, role, content) "
                "VALUES (:id, 'user', :c)"
            ),
            {"id": sid, "c": f"message {i}"},
        )


def _snapshot(db, snap_id, source_session_id, *, age_days=0):
    db.execute(
        text(
            "INSERT INTO grimoire_chat.shared_snapshots "
            "(id, created_at, transcript, message_count, source_session_id) "
            "VALUES (:id, :created, '[]'::jsonb, 0, :src)"
        ),
        {
            "id": snap_id,
            "created": NOW - timedelta(days=age_days),
            "src": source_session_id,
        },
    )


def _count(db, table, column, value):
    return db.execute(
        text(f"SELECT count(*) FROM grimoire_chat.{table} WHERE {column} = :v"),
        {"v": value},
    ).scalar_one()


def _audit_since(db, last_id):
    return db.execute(
        text(
            "SELECT action, selector_sha256, sessions_deleted, messages_deleted, "
            "snapshots_deleted FROM grimoire_chat.purge_audit WHERE id > :last "
            "ORDER BY id"
        ),
        {"last": last_id},
    ).all()


def _last_audit_id(db):
    return db.execute(
        text("SELECT coalesce(max(id), 0) FROM grimoire_chat.purge_audit")
    ).scalar_one()


def test_retention_windows_are_pinned():
    assert retention.SESSION_RETENTION_DAYS == 30
    assert retention.SNAPSHOT_RETENTION_DAYS == 365


def test_window_env_overrides_and_rejects_below_one(monkeypatch):
    monkeypatch.setenv("CHAT_PUBLIC_SESSION_RETENTION_DAYS", "7")
    assert retention.session_retention_days() == 7
    monkeypatch.setenv("CHAT_PUBLIC_SNAPSHOT_RETENTION_DAYS", "0")
    with pytest.raises(ValueError):
        retention.snapshot_retention_days()
    monkeypatch.delenv("CHAT_PUBLIC_SESSION_RETENTION_DAYS")
    assert retention.session_retention_days() == 30


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"session_id": "a", "ip_hash": "b"}, {"session_id": "", "ip_hash": ""}],
)
def test_takedown_needs_exactly_one_selector(db, kwargs):
    with pytest.raises(ValueError):
        retention.takedown(db, **kwargs)


def test_takedown_by_session_id_removes_everything_and_audits_digest(db):
    sid = _uid("td-sess")
    other = _uid("td-other")
    _session(db, sid, messages=3)
    _session(db, other, messages=1)
    _snapshot(db, _uid("snap"), sid)
    _snapshot(db, _uid("snap"), sid)
    _snapshot(db, _uid("snap"), other)
    last = _last_audit_id(db)

    report = retention.takedown(db, session_id=sid)

    assert (
        report.sessions_deleted,
        report.messages_deleted,
        report.snapshots_deleted,
    ) == (1, 3, 2)
    assert _count(db, "sessions", "id", sid) == 0
    assert _count(db, "messages", "session_id", sid) == 0
    assert _count(db, "shared_snapshots", "source_session_id", sid) == 0
    # The other session and its snapshot are untouched.
    assert _count(db, "sessions", "id", other) == 1
    assert _count(db, "shared_snapshots", "source_session_id", other) == 1

    rows = _audit_since(db, last)
    assert rows == [
        (
            "takedown_session",
            hashlib.sha256(sid.encode()).hexdigest(),
            1,
            3,
            2,
        )
    ]
    assert sid not in rows[0]


def test_takedown_by_ip_hash_removes_only_matching_sessions(db):
    ip = _uid("iphash")
    a, b, c = _uid("ip-a"), _uid("ip-b"), _uid("ip-c")
    _session(db, a, ip_hash=ip, messages=2)
    _session(db, b, ip_hash=ip, messages=1)
    _session(db, c, ip_hash=_uid("iphash-other"), messages=4)
    _snapshot(db, _uid("snap"), a)
    _snapshot(db, _uid("snap"), b)
    _snapshot(db, _uid("snap"), c)
    last = _last_audit_id(db)

    report = retention.takedown(db, ip_hash=ip)

    assert (
        report.sessions_deleted,
        report.messages_deleted,
        report.snapshots_deleted,
    ) == (2, 3, 2)
    assert _count(db, "sessions", "ip_hash", ip) == 0
    for sid in (a, b):
        assert _count(db, "messages", "session_id", sid) == 0
        assert _count(db, "shared_snapshots", "source_session_id", sid) == 0
    assert _count(db, "sessions", "id", c) == 1
    assert _count(db, "messages", "session_id", c) == 4
    assert _count(db, "shared_snapshots", "source_session_id", c) == 1
    assert _audit_since(db, last) == [
        ("takedown_ip_hash", hashlib.sha256(ip.encode()).hexdigest(), 2, 3, 2)
    ]


def test_takedown_matching_nothing_still_writes_zero_audit_row(db):
    sid = _uid("missing")
    last = _last_audit_id(db)

    report = retention.takedown(db, session_id=sid)

    assert report.sessions_deleted == report.messages_deleted == 0
    assert report.snapshots_deleted == 0
    assert _audit_since(db, last) == [
        ("takedown_session", hashlib.sha256(sid.encode()).hexdigest(), 0, 0, 0)
    ]


def test_retention_deletes_idle_keeps_recent_and_shared(db):
    idle = _uid("idle31")
    recent = _uid("idle29")
    edge = _uid("idle30")
    shared = _uid("shared")
    _session(db, idle, idle_days=31, messages=2)
    _session(db, recent, idle_days=29, messages=1)
    _session(db, edge, idle_days=30, messages=1)
    _session(db, shared, idle_days=31, messages=1)
    snap = _uid("snap")
    _snapshot(db, snap, shared, age_days=100)
    last = _last_audit_id(db)

    report = retention.purge_expired(db, now=NOW)

    assert _count(db, "sessions", "id", idle) == 0
    assert _count(db, "messages", "session_id", idle) == 0
    assert _count(db, "sessions", "id", recent) == 1
    assert _count(db, "messages", "session_id", recent) == 1
    assert _count(db, "sessions", "id", edge) == 1
    assert _count(db, "messages", "session_id", edge) == 1
    assert _count(db, "sessions", "id", shared) == 1
    assert _count(db, "shared_snapshots", "id", snap) == 1
    assert report.action == "retention"
    assert report.sessions_deleted >= 1
    assert report.messages_deleted >= 2
    rows = _audit_since(db, last)
    assert len(rows) == 1
    assert rows[0][0] == "retention"
    assert rows[0][1] is None
    assert rows[0][2:] == (
        report.sessions_deleted,
        report.messages_deleted,
        report.snapshots_deleted,
    )


def test_retention_deletes_expired_snapshot_and_its_idle_session_together(db):
    sid = _uid("old-shared")
    snap = _uid("snap-old")
    _session(db, sid, idle_days=400, messages=2)
    _snapshot(db, snap, sid, age_days=366)

    report = retention.purge_expired(db, now=NOW)

    assert _count(db, "shared_snapshots", "id", snap) == 0
    assert _count(db, "sessions", "id", sid) == 0
    assert _count(db, "messages", "session_id", sid) == 0
    assert report.snapshots_deleted >= 1


def test_snapshot_exactly_at_window_is_kept(db):
    sid = _uid("edge")
    snap = _uid("snap-edge")
    _session(db, sid, idle_days=400)
    _snapshot(db, snap, sid, age_days=365)

    retention.purge_expired(db, now=NOW)

    assert _count(db, "shared_snapshots", "id", snap) == 1
    assert _count(db, "sessions", "id", sid) == 1


def test_expired_snapshot_does_not_hide_current_snapshot_protection(db):
    sid, expired, current = _uid("mixed"), _uid("expired"), _uid("current")
    _session(db, sid, idle_days=400, messages=1)
    _snapshot(db, expired, sid, age_days=366)
    _snapshot(db, current, sid)

    report = retention.purge_expired(db, now=NOW)

    assert report.snapshots_deleted == 1
    assert _count(db, "shared_snapshots", "id", expired) == 0
    assert _count(db, "shared_snapshots", "id", current) == 1
    assert _count(db, "sessions", "id", sid) == 1


def test_takedown_after_retention_removes_kept_sessions_snapshot(db):
    sid = _uid("e2e")
    snap = _uid("snap-e2e")
    _session(db, sid, idle_days=45, messages=2)
    _snapshot(db, snap, sid, age_days=10)

    retention.purge_expired(db, now=NOW)
    # Retention kept the shared session, so the snapshot is still resolvable.
    assert _count(db, "sessions", "id", sid) == 1
    assert _count(db, "shared_snapshots", "id", snap) == 1

    report = retention.takedown(db, session_id=sid)

    assert report.snapshots_deleted == 1
    assert _count(db, "shared_snapshots", "id", snap) == 0
    assert _count(db, "shared_snapshots", "source_session_id", sid) == 0
    assert _count(db, "sessions", "id", sid) == 0


@pytest.mark.parametrize("role", ["public_writer", "public_reader"])
def test_public_roles_cannot_read_or_alter_purge_audit(pg, role):
    engine = create_engine(pg.url)
    try:
        for statement in (
            "SELECT count(*) FROM grimoire_chat.purge_audit",
            "INSERT INTO grimoire_chat.purge_audit (action) VALUES ('retention')",
            "UPDATE grimoire_chat.purge_audit SET sessions_deleted = 0",
            "DELETE FROM grimoire_chat.purge_audit",
            "TRUNCATE grimoire_chat.purge_audit",
            "SELECT nextval('grimoire_chat.purge_audit_id_seq')",
            "SELECT last_value FROM grimoire_chat.purge_audit_id_seq",
            "SELECT setval('grimoire_chat.purge_audit_id_seq', 1)",
        ):
            with Session(engine) as session:
                # Fixed role names from the parametrization, never user input.
                session.execute(text(f"SET ROLE {role}"))
                with pytest.raises(ProgrammingError, match="permission denied"):
                    session.execute(text(statement))
                session.rollback()
    finally:
        engine.dispose()


def test_existing_public_chat_grants_still_work(db):
    sid, snap = _uid("grant"), _uid("grant-snapshot")
    db.execute(text("SET LOCAL ROLE public_writer"))
    _session(db, sid, messages=1)
    _snapshot(db, snap, sid)
    assert _count(db, "messages", "session_id", sid) == 1
    db.execute(
        text("UPDATE grimoire_chat.sessions SET turn_count = 1 WHERE id = :id"),
        {"id": sid},
    )
    db.execute(
        text("DELETE FROM grimoire_chat.messages WHERE session_id = :id"), {"id": sid}
    )
    db.execute(text("RESET ROLE"))
    db.execute(text("SET LOCAL ROLE public_reader"))
    assert _count(db, "shared_snapshots", "id", snap) == 1
    db.execute(text("RESET ROLE"))


@pytest.mark.parametrize(
    "values",
    [
        "'unknown', NULL, 0, 0, 0",
        "'retention', NULL, -1, 0, 0",
        "'retention', NULL, 0, -1, 0",
        "'retention', NULL, 0, 0, -1",
        "'takedown_session', 'raw-session-id', 0, 0, 0",
    ],
)
def test_audit_constraints(db, values):
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                "INSERT INTO grimoire_chat.purge_audit "
                "(action, selector_sha256, sessions_deleted, messages_deleted, snapshots_deleted) "
                f"VALUES ({values})"
            )
        )


def test_just_expired_boundaries_and_last_seen_not_created(db):
    idle, active, shared = _uid("just-idle"), _uid("active"), _uid("just-shared")
    _session(db, idle, idle_days=30)
    _session(db, active, idle_days=400)
    _session(db, shared, idle_days=400)
    db.execute(
        text(
            "UPDATE grimoire_chat.sessions SET last_seen_at = last_seen_at - interval '1 microsecond' WHERE id = :id"
        ),
        {"id": idle},
    )
    db.execute(
        text("UPDATE grimoire_chat.sessions SET last_seen_at = :now WHERE id = :id"),
        {"now": NOW, "id": active},
    )
    snap = _uid("just-expired")
    _snapshot(db, snap, shared, age_days=365)
    db.execute(
        text(
            "UPDATE grimoire_chat.shared_snapshots SET created_at = created_at - interval '1 microsecond' WHERE id = :id"
        ),
        {"id": snap},
    )

    retention.purge_expired(db, now=NOW)

    assert _count(db, "sessions", "id", idle) == 0
    assert _count(db, "sessions", "id", active) == 1
    assert _count(db, "sessions", "id", shared) == 0
    assert _count(db, "shared_snapshots", "id", snap) == 0


@pytest.mark.parametrize("operation", ["retention", "session", "ip"])
def test_purge_preserves_legacy_cache_and_unrelated_sessions(db, operation):
    sid, ip, fork = _uid("target"), _uid("stored-hash"), _uid("fork")
    _session(db, sid, idle_days=400, ip_hash=ip, messages=1)
    _session(db, fork, messages=1)
    _snapshot(db, _uid("expired"), sid, age_days=400)
    db.execute(
        text(
            "INSERT INTO chat_public.sessions (id, last_seen_at, ip_hash) VALUES (:id, :seen, :ip)"
        ),
        {"id": sid, "seen": NOW - timedelta(days=400), "ip": ip},
    )
    db.execute(
        text(
            "INSERT INTO chat_public.messages (session_id, role, content) VALUES (:id, 'user', 'legacy')"
        ),
        {"id": sid},
    )
    db.execute(
        text(
            "INSERT INTO chat_public.shared_snapshots (id, source_session_id, transcript, created_at) VALUES (:id, :id, '[]', :seen)"
        ),
        {"id": sid, "seen": NOW - timedelta(days=400)},
    )
    db.execute(
        text(
            "INSERT INTO grimoire_chat.response_cache (cache_key, normalized_message, prompt_version, notes_watermark, response_text) VALUES (:id, 'question', 'v1', 'v1', 'answer')"
        ),
        {"id": sid},
    )
    legacy_audits = db.execute(
        text("SELECT count(*) FROM chat_public.purge_audit")
    ).scalar_one()
    if operation == "retention":
        retention.purge_expired(db, now=NOW)
    elif operation == "session":
        retention.takedown(db, session_id=sid)
    else:
        retention.takedown(db, ip_hash=ip)
    assert _count(db, "sessions", "id", sid) == 0
    assert _count(db, "sessions", "id", fork) == 1
    assert _count(db, "messages", "session_id", fork) == 1
    assert _count(db, "response_cache", "cache_key", sid) == 1
    for table, column in (
        ("sessions", "id"),
        ("messages", "session_id"),
        ("shared_snapshots", "source_session_id"),
    ):
        assert (
            db.execute(
                text(f"SELECT count(*) FROM chat_public.{table} WHERE {column} = :id"),
                {"id": sid},
            ).scalar_one()
            == 1
        )
    assert (
        db.execute(text("SELECT count(*) FROM chat_public.purge_audit")).scalar_one()
        == legacy_audits
    )


@pytest.mark.parametrize("command", ["chat-public-retention", "session", "ip"])
@pytest.mark.parametrize("outcome", ["commit", "dry-run", "audit-failure"])
def test_real_jobs_cli_transaction(pg, monkeypatch, command, outcome):
    """Exercise the actual CLI import and core against migrated Postgres."""
    from app import jobs_main

    engine = create_engine(pg.url)
    sid, snap, ip = _uid("cli"), _uid("cli-snap"), _uid("stored-hash")
    digest = hashlib.sha256((ip if command == "ip" else sid).encode()).hexdigest()
    monkeypatch.setattr("core.db.get_engine", lambda: engine)
    monkeypatch.setattr(jobs_main, "configure_logging", lambda: None)
    args = [
        "chat-public-takedown",
        "--ip-hash" if command == "ip" else "--session-id",
        ip if command == "ip" else sid,
    ]
    if command == "chat-public-retention":
        args = [command]
    if outcome == "dry-run":
        args.append("--dry-run")
    if outcome == "audit-failure":

        def fail_audit(session, report, selector=None):
            session.execute(
                text(
                    "INSERT INTO grimoire_chat.purge_audit (action) VALUES ('invalid')"
                )
            )

        monkeypatch.setattr(retention, "_write_audit", fail_audit)
    try:
        with Session(engine) as seed:
            _session(seed, sid, messages=2, ip_hash=ip)
            _snapshot(seed, snap, sid)
            # Relative to the database clock so the real CLI needs no clock mock.
            seed.execute(
                text(
                    "UPDATE grimoire_chat.sessions SET last_seen_at = now() - interval '400 days' WHERE id = :id"
                ),
                {"id": sid},
            )
            seed.execute(
                text(
                    "UPDATE grimoire_chat.shared_snapshots SET created_at = now() - interval '400 days' WHERE id = :id"
                ),
                {"id": snap},
            )
            last = _last_audit_id(seed)
            seed.commit()
        result = CliRunner().invoke(jobs_main.app, args)
        assert (result.exit_code == 0) == (outcome != "audit-failure"), result.output
        assert sid not in result.output
        assert ip not in result.output
        with Session(engine) as check:
            remains = 0 if outcome == "commit" else 1
            assert _count(check, "sessions", "id", sid) == remains
            assert _count(check, "messages", "session_id", sid) == 2 * remains
            assert _count(check, "shared_snapshots", "id", snap) == remains
            audits = _audit_since(check, last)
            if outcome == "commit":
                assert len(audits) == 1
                expected_action = {
                    "session": "takedown_session",
                    "ip": "takedown_ip_hash",
                    "chat-public-retention": "retention",
                }[command]
                assert tuple(audits[0]) == (
                    expected_action,
                    None if command == "chat-public-retention" else digest,
                    1,
                    2,
                    1,
                )
            else:
                assert audits == []
            if result.exit_code == 0:
                payload = json.loads(result.stdout)
                assert payload["sessions_deleted"] == 1
                assert payload["dry_run"] == (outcome == "dry-run")
    finally:
        # Remove only this test's synthetic records from the shared test server.
        with Session(engine) as cleanup:
            cleanup.execute(
                text("DELETE FROM grimoire_chat.shared_snapshots WHERE id = :id"),
                {"id": snap},
            )
            cleanup.execute(
                text("DELETE FROM grimoire_chat.sessions WHERE id = :id"), {"id": sid}
            )
            cleanup.execute(
                text("DELETE FROM grimoire_chat.purge_audit WHERE id > :last"),
                {"last": last},
            )
            cleanup.commit()
        engine.dispose()


@pytest.mark.parametrize("selector", ["session", "ip"])
def test_takedown_fences_concurrent_share_before_deleting_children(pg, selector):
    """A second connection cannot create a SET NULL orphan during takedown."""
    engine = create_engine(pg.url)
    sid, ip = _uid("race-session"), _uid("race-hash")
    original, concurrent = _uid("original-share"), _uid("concurrent-share")
    deleted_children = threading.Event()
    resume = threading.Event()
    with Session(engine) as seed:
        _session(seed, sid, ip_hash=ip, messages=1)
        _snapshot(seed, original, sid)
        last = _last_audit_id(seed)
        seed.commit()

    def pause_after_snapshots(conn, cursor, statement, parameters, context, many):
        if statement.startswith("DELETE FROM grimoire_chat.shared_snapshots"):
            deleted_children.set()
            if not resume.wait(timeout=10):
                raise TimeoutError("concurrent share test did not release takedown")

    def purge():
        # Each thread owns its Session; only the engine is shared.
        with Session(engine) as session:
            kwargs = {"session_id": sid} if selector == "session" else {"ip_hash": ip}
            report = retention.takedown(session, **kwargs)
            session.commit()
            return report

    event.listen(engine, "after_cursor_execute", pause_after_snapshots)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(purge)
            try:
                assert deleted_children.wait(timeout=5)
                with Session(engine) as share:
                    share.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    with pytest.raises(OperationalError, match="lock timeout"):
                        _snapshot(share, concurrent, sid)
                    share.rollback()
            finally:
                resume.set()
            report = future.result(timeout=10)
        assert (
            report.sessions_deleted,
            report.messages_deleted,
            report.snapshots_deleted,
        ) == (1, 1, 1)
        with Session(engine) as check:
            assert _count(check, "sessions", "id", sid) == 0
            assert _count(check, "shared_snapshots", "id", original) == 0
            assert _count(check, "shared_snapshots", "id", concurrent) == 0
    finally:
        resume.set()
        event.remove(engine, "after_cursor_execute", pause_after_snapshots)
        with Session(engine) as cleanup:
            cleanup.execute(
                text("DELETE FROM grimoire_chat.shared_snapshots WHERE id IN (:a, :b)"),
                {"a": original, "b": concurrent},
            )
            cleanup.execute(
                text("DELETE FROM grimoire_chat.sessions WHERE id = :id"), {"id": sid}
            )
            cleanup.execute(
                text("DELETE FROM grimoire_chat.purge_audit WHERE id > :last"),
                {"last": last},
            )
            cleanup.commit()
        engine.dispose()
