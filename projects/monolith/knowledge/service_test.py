"""Tests for knowledge service startup registration and handlers.

The vault-coupled handlers (reconcile, vault-backup, classify-gaps,
research-gaps, detect-drift) and the vault git-clone/sentinel bootstrap were
removed with the Obsidian decommission (ADR 006). What survives is the
fileless gap discovery handler and the pure-Postgres graph layout pass.
"""

import logging
import math
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge import service
from knowledge.models import Note, NoteLink
from knowledge.service import on_startup


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with Postgres schema names stripped."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas: dict[str, str] = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(name="session")
def session_fixture(engine):
    from scheduler.api import _registry

    _registry.clear()
    with Session(engine) as db:
        yield db
    _registry.clear()


def _add_note(
    session: Session,
    note_id: str,
    *,
    title: str | None = None,
    visibility: str | None = None,
) -> Note:
    note = Note(
        note_id=note_id,
        path=f"_processed/{note_id}.md",
        title=title or note_id,
        content_hash=f"hash-{note_id}",
        type="atom",
        visibility=visibility,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def _link(session: Session, *, src_fk: int, target_id: str) -> None:
    session.add(
        NoteLink(
            src_note_fk=src_fk,
            target_id=target_id,
            target_title=target_id,
            kind="link",
            edge_type=None,
        )
    )
    session.commit()


class TestOnStartup:
    def _registered_names(self) -> set[str]:
        """Run on_startup with register_job patched; return the job names.

        Patches ``scheduler.api.register_job`` (the name on_startup imports at
        call time) so the test never touches the schema-qualified
        ``scheduler.scheduled_jobs`` table, which the SQLite create_all fixture
        cannot resolve.
        """
        with patch("scheduler.api.register_job") as mock_register:
            on_startup(MagicMock())
        return {call.kwargs["name"] for call in mock_register.call_args_list}

    def test_registers_fileless_jobs(self):
        names = self._registered_names()
        assert "knowledge.layout" in names
        assert "knowledge.ingest" in names
        assert "knowledge.discover-gaps" in names

    def test_does_not_register_vault_jobs(self):
        names = self._registered_names()
        for removed in (
            "knowledge.reconcile",
            "knowledge.vault-backup",
            "knowledge.classify-gaps",
            "knowledge.research-gaps",
            "knowledge.detect-drift",
        ):
            assert removed not in names


class TestLayoutHandler:
    """The pure-Postgres graph layout pass (formerly hosted in the reconciler).

    Each test seeds Note/NoteLink rows directly (no vault) and drives
    ``layout_handler`` against the in-memory SQLite engine.
    """

    @pytest.mark.asyncio
    async def test_populates_layout_positions(self, session):
        a = _add_note(session, "a", title="A")
        b = _add_note(session, "b", title="B")
        _link(session, src_fk=a.id, target_id="b")
        _link(session, src_fk=b.id, target_id="a")

        await service.layout_handler(session)

        notes = list(session.scalars(select(Note).where(Note.deleted_at.is_(None))))
        assert len(notes) == 2
        for note in notes:
            assert note.layout_x is not None, f"{note.note_id} has no layout_x"
            assert note.layout_y is not None, f"{note.note_id} has no layout_y"
            assert math.isfinite(note.layout_x)
            assert math.isfinite(note.layout_y)

    @pytest.mark.asyncio
    async def test_full_layout_failure_is_isolated(self, session, monkeypatch, caplog):
        a = _add_note(session, "a", title="A")
        _link(session, src_fk=a.id, target_id="b")

        def boom(_engine):
            raise RuntimeError("boom")

        monkeypatch.setattr("knowledge.service._run_layout_pass", boom)

        with caplog.at_level(logging.ERROR, logger="knowledge.service"):
            result = await service.layout_handler(session)

        assert result is None
        assert any("knowledge.layout: pass failed" in r.message for r in caplog.records)
        notes = list(session.scalars(select(Note).where(Note.deleted_at.is_(None))))
        assert len(notes) == 1
        assert notes[0].layout_x is None
        assert notes[0].layout_y is None

    @pytest.mark.asyncio
    async def test_populates_public_layout_positions(self, session):
        pub = _add_note(session, "pub", title="Pub", visibility="public")
        pub2 = _add_note(session, "pub2", title="Pub2", visibility="public")
        _add_note(session, "priv", title="Priv", visibility="private")
        _link(session, src_fk=pub.id, target_id="pub2")
        _link(session, src_fk=pub2.id, target_id="pub")
        _link(session, src_fk=pub.id, target_id="priv")

        await service.layout_handler(session)

        notes = {
            n.note_id: n
            for n in session.scalars(select(Note).where(Note.deleted_at.is_(None)))
        }
        assert set(notes) == {"pub", "pub2", "priv"}
        for nid in ("pub", "pub2"):
            assert notes[nid].layout_x_public is not None
            assert notes[nid].layout_y_public is not None
            assert math.isfinite(notes[nid].layout_x_public)
            assert math.isfinite(notes[nid].layout_y_public)
        assert notes["priv"].layout_x_public is None
        assert notes["priv"].layout_y_public is None

    @pytest.mark.asyncio
    async def test_public_layout_failure_isolated(self, session, monkeypatch, caplog):
        a = _add_note(session, "a", title="A", visibility="public")
        b = _add_note(session, "b", title="B", visibility="public")
        _link(session, src_fk=a.id, target_id="b")
        _link(session, src_fk=b.id, target_id="a")

        def boom(_engine):
            raise RuntimeError("boom")

        monkeypatch.setattr("knowledge.service._run_public_layout_pass", boom)

        with caplog.at_level(logging.ERROR, logger="knowledge.service"):
            result = await service.layout_handler(session)

        assert result is None
        assert any(
            "knowledge.layout: public pass failed" in r.message for r in caplog.records
        )
        notes = list(session.scalars(select(Note).where(Note.deleted_at.is_(None))))
        assert len(notes) == 2
        for note in notes:
            assert note.layout_x is not None
            assert note.layout_y is not None
            assert note.layout_x_public is None
            assert note.layout_y_public is None


class TestDiscoverGapsHandler:
    """The fileless discover-gaps handler delegates to its own session."""

    @pytest.mark.asyncio
    async def test_inserts_gap_for_unresolved_link(self, engine, session, monkeypatch):
        from knowledge.models import Gap

        src = _add_note(session, "src", title="Src")
        _link(session, src_fk=src.id, target_id="missing-term")

        # The sync core opens its own Session(get_engine()); point it at the
        # test engine so the worker thread sees the seeded rows.
        monkeypatch.setattr("knowledge.service.get_engine", lambda: engine)

        result = await service.discover_gaps_handler(MagicMock())

        assert result is None
        gaps = (
            session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
        )
        assert [g.term for g in gaps] == ["missing-term"]
