"""Real-Postgres tests for public chat retention and takedown (ADR security/005).

The `pg` fixture applies every migration and is shared across tests, so every
test uses unique ids and asserts only on its own rows. Hand-written bdd_test
(chat_public is gazelle-excluded), registered in projects/monolith/BUILD.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import ProgrammingError
from sqlmodel import Session, create_engine, text

from chat_public import retention

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
            "INSERT INTO chat_public.sessions (id, created_at, last_seen_at, ip_hash) "
            "VALUES (:id, :seen, :seen, :ip)"
        ),
        {"id": sid, "seen": NOW - timedelta(days=idle_days), "ip": ip_hash},
    )
    for i in range(messages):
        db.execute(
            text(
                "INSERT INTO chat_public.messages (session_id, role, content) "
                "VALUES (:id, 'user', :c)"
            ),
            {"id": sid, "c": f"message {i}"},
        )


def _snapshot(db, snap_id, source_session_id, *, age_days=0):
    db.execute(
        text(
            "INSERT INTO chat_public.shared_snapshots "
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
        text(f"SELECT count(*) FROM chat_public.{table} WHERE {column} = :v"),
        {"v": value},
    ).scalar_one()


def _audit_since(db, last_id):
    return db.execute(
        text(
            "SELECT action, selector_sha256, sessions_deleted, messages_deleted, "
            "snapshots_deleted FROM chat_public.purge_audit WHERE id > :last "
            "ORDER BY id"
        ),
        {"last": last_id},
    ).all()


def _last_audit_id(db):
    return db.execute(
        text("SELECT coalesce(max(id), 0) FROM chat_public.purge_audit")
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


def test_public_writer_cannot_read_or_write_purge_audit(pg):
    engine = create_engine(pg.url)
    try:
        for statement in (
            "SELECT count(*) FROM chat_public.purge_audit",
            "INSERT INTO chat_public.purge_audit (action) VALUES ('retention')",
        ):
            with Session(engine) as session:
                session.execute(text("SET ROLE public_writer"))
                with pytest.raises(ProgrammingError, match="permission denied"):
                    session.execute(text(statement))
                session.rollback()
    finally:
        engine.dispose()
