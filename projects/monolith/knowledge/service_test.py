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
