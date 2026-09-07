"""Policy and mutation tests for the public-fact publication lane."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge import publish as publish_module
from knowledge.models import Dispute, Note
from knowledge.notes import list_notes_for_review
from knowledge.publish import (
    PUBLISHABLE_SCOPES,
    PublishReport,
    apply,
    select_publishable,
    select_unpublishable,
)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    """Use a file-backed SQLite database for hermetic publication tests."""
    engine = create_engine(f"sqlite:///{tmp_path / 'publish.db'}")
    original_schemas: dict[str, str] = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]
        engine.dispose()


def _note(note_id: str, **overrides) -> Note:
    values = {
        "note_id": note_id,
        "path": f"facts/{note_id}.md",
        "title": f"Fact {note_id}",
        "content_hash": f"hash-{note_id}",
        "content": "Safe fact content.",
        "type": "fact",
        "visibility": "private",
        "verification_state": "verified",
        "scope": "repo:jomcgi-org/homelab",
    }
    values.update(overrides)
    return Note(**values)


def _save(session: Session, *rows) -> None:
    session.add_all(rows)
    session.commit()


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("verified", True),
        ("unverified", True),
        ("legacy", False),
        ("disputed", False),
        ("invalidated", False),
    ],
)
def test_select_publishable_filters_verification_state(session, state, expected):
    _save(session, _note("candidate", verification_state=state))
    assert (select_publishable(session)[0] == ["candidate"]) is expected


@pytest.mark.parametrize(
    ("scope", "expected"),
    [(scope, True) for scope in sorted(PUBLISHABLE_SCOPES)]
    + [("repo:elsewhere/project", False), (None, False)],
)
def test_select_publishable_filters_scope(session, scope, expected):
    _save(session, _note("candidate", scope=scope))
    assert (select_publishable(session)[0] == ["candidate"]) is expected


@pytest.mark.parametrize(("type_", "expected"), [("fact", True), ("atom", False)])
def test_select_publishable_filters_type(session, type_, expected):
    _save(session, _note("candidate", type=type_))
    assert (select_publishable(session)[0] == ["candidate"]) is expected


def test_human_hold_skipped(session):
    _save(
        session,
        _note("human-hold", visibility="private", visibility_verified=True),
    )

    assert select_publishable(session) == ([], 0)
    assert apply(session, dry_run=False).published == 0


@pytest.mark.parametrize("field", ["title", "content"])
def test_select_publishable_skips_redaction_without_logging_secret(
    session, caplog, field
):
    secret = "ghp_12345678901234567890"
    _save(session, _note("secret", **{field: f"credential {secret}"}))

    assert select_publishable(session) == ([], 1)
    assert "note_id=secret" in caplog.text
    assert "reason=redaction_hit" in caplog.text
    assert secret not in caplog.text


def test_select_publishable_filters_open_dispute(session):
    _save(
        session,
        _note("disputed-fact"),
        Dispute(note_id="disputed-fact", reason="Needs review", state="open"),
    )
    assert select_publishable(session) == ([], 0)
    assert apply(session, dry_run=True).skipped_dispute == 1


def test_select_unpublishable_soft_deleted(session):
    _save(
        session,
        _note(
            "deleted",
            visibility="public",
            published_at=datetime.now(timezone.utc),
            deleted_at=datetime.now(timezone.utc),
        ),
    )
    assert select_unpublishable(session) == ["deleted"]


@pytest.mark.parametrize("state", ["invalidated", "disputed"])
def test_select_unpublishable_verification_state(session, state):
    _save(
        session,
        _note(
            state,
            visibility="public",
            verification_state=state,
            published_at=datetime.now(timezone.utc),
        ),
    )
    assert select_unpublishable(session) == [state]


def test_select_unpublishable_open_dispute(session):
    _save(
        session,
        _note(
            "open-dispute",
            visibility="public",
            published_at=datetime.now(timezone.utc),
        ),
        Dispute(note_id="open-dispute", reason="Needs review", state="open"),
    )
    assert select_unpublishable(session) == ["open-dispute"]


def test_apply_publishes_note(session):
    note = _note("publish-me")
    _save(session, note)

    assert apply(session, dry_run=False) == PublishReport(1, 0, 0, 0)
    session.refresh(note)
    assert note.visibility == "public"
    assert note.visibility_verified is False
    assert note.published_at is not None


def test_audit_queue_membership(session):
    note = _note("audit-me")
    _save(session, note)

    assert apply(session, dry_run=False).published == 1

    session.refresh(note)
    assert note.visibility == "public"
    assert note.visibility_verified is False
    assert [row["id"] for row in list_notes_for_review(session, mode="audit")] == [
        "audit-me"
    ]


def test_human_published_never_unpublished(session):
    note = _note(
        "human-public",
        visibility="public",
        visibility_verified=True,
        verification_state="invalidated",
        published_at=datetime.now(timezone.utc),
    )
    _save(session, note)

    assert select_unpublishable(session) == []
    assert apply(session, dry_run=False).unpublished == 0
    session.refresh(note)
    assert note.visibility == "public"
    assert note.visibility_verified is True
    assert note.published_at is not None


def test_dispute_opened_between_select_and_update(session, monkeypatch):
    note = _note("raced-dispute")
    _save(session, note)
    original_select = publish_module.select_publishable

    def select_then_dispute(active_session):
        selected = original_select(active_session)
        active_session.add(
            Dispute(note_id="raced-dispute", reason="Concurrent dispute")
        )
        active_session.flush()
        return selected

    monkeypatch.setattr(publish_module, "select_publishable", select_then_dispute)

    assert apply(session, dry_run=False).published == 0
    session.refresh(note)
    assert note.visibility == "private"
    assert note.published_at is None


def test_apply_unpublishes_note(session):
    note = _note(
        "unpublish-me",
        visibility="public",
        verification_state="invalidated",
        published_at=datetime.now(timezone.utc),
    )
    _save(session, note)

    assert apply(session, dry_run=False).unpublished == 1
    session.refresh(note)
    assert note.visibility == "private"
    assert note.visibility_verified is False
    assert note.published_at is None


def test_scope_leaves_set_unpublish(session):
    note = _note(
        "scope-left-policy",
        visibility="public",
        scope="repo:elsewhere/project",
        published_at=datetime.now(timezone.utc),
    )
    _save(session, note)

    assert apply(session, dry_run=False).unpublished == 1
    session.refresh(note)
    assert note.visibility == "private"
    assert note.published_at is None


def test_dispute_opened_then_closed_then_republish(session):
    note = _note("dispute-cycle")
    _save(session, note)
    assert apply(session, dry_run=False).published == 1

    dispute = Dispute(note_id=note.note_id, reason="Needs review")
    _save(session, dispute)
    assert apply(session, dry_run=False).unpublished == 1
    session.refresh(note)
    assert note.visibility == "private"
    assert note.published_at is None

    dispute.state = "rejected"
    session.add(dispute)
    session.commit()
    assert apply(session, dry_run=False).published == 1
    session.refresh(note)
    assert note.visibility == "public"
    assert note.visibility_verified is False
    assert note.published_at is not None


def test_unpublish_idempotent(session):
    note = _note(
        "unpublish-once",
        visibility="public",
        verification_state="invalidated",
        published_at=datetime.now(timezone.utc),
    )
    _save(session, note)

    assert apply(session, dry_run=False).unpublished == 1
    assert apply(session, dry_run=False).unpublished == 0


def test_apply_is_idempotent(session):
    _save(session, _note("once"))
    assert apply(session, dry_run=False).published == 1
    assert apply(session, dry_run=False).published == 0


def test_apply_chunks_updates_in_500_note_batches(session, monkeypatch):
    _save(session, *(_note(f"fact-{index}") for index in range(501)))
    original_commit = session.commit
    commits = 0

    def counted_commit() -> None:
        nonlocal commits
        commits += 1
        original_commit()

    monkeypatch.setattr(session, "commit", counted_commit)
    assert apply(session, dry_run=False).published == 501
    assert commits == 2


def test_apply_dry_run_reports_without_changes(session):
    publish = _note("publish")
    unpublish = _note(
        "unpublish",
        visibility="public",
        verification_state="invalidated",
        published_at=datetime.now(timezone.utc),
    )
    _save(session, publish, unpublish)

    assert apply(session, dry_run=True) == PublishReport(1, 1, 0, 0)
    session.refresh(publish)
    session.refresh(unpublish)
    assert publish.visibility == "private"
    assert publish.published_at is None
    assert unpublish.visibility == "public"
    assert unpublish.published_at is not None


def test_legacy_notes_are_untouched(session):
    private = _note("legacy-private", verification_state="legacy")
    public = _note(
        "legacy-public",
        visibility="public",
        verification_state="legacy",
        scope="repo:outside/scope",
        published_at=datetime.now(timezone.utc),
    )
    _save(session, private, public)

    assert select_publishable(session) == ([], 0)
    assert select_unpublishable(session) == []
    assert apply(session, dry_run=False) == PublishReport(0, 0, 0, 0)
    rows = session.exec(select(Note).where(Note.note_id.like("legacy-%"))).all()
    by_id = {row.note_id: row for row in rows}
    assert by_id["legacy-private"].visibility == "private"
    assert by_id["legacy-public"].visibility == "public"
    assert by_id["legacy-public"].published_at is not None
