"""Tests for the gardener's completion distillation phase."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.gardener import GARDENER_VERSION, Gardener, _DISTILL_PROMPT
from knowledge.models import AtomRawProvenance, Note
from knowledge.visibility import VISIBILITY_CRITERIA


def test_distill_prompt_includes_visibility_criteria():
    """The distill prompt must require a `visibility:` frontmatter field
    and embed the verbatim criteria text from `knowledge.visibility`.

    Distilled atoms come from completed task notes — typically workplace
    specifics — so the criteria steer most outputs to `private`. Drift
    between this prompt and the public-route filters is the primary leak
    failure mode, hence the assertion on the imported constant.
    """
    rendered = _DISTILL_PROMPT.format(
        note_id="n", note_path="x", processed_root="p", title="t"
    )
    assert "visibility:" in rendered
    # criteria must be appended verbatim — drift = leak
    assert VISIBILITY_CRITERIA.strip() in rendered


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
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


def _make_task(
    session: Session,
    tmp_path: Path,
    note_id: str,
    title: str = "Task",
    *,
    status: str = "done",
) -> Note:
    """Create a task note in the DB and on disk."""
    rel_path = f"_processed/{note_id}.md"
    extra = {"status": status}
    note = Note(
        note_id=note_id,
        path=rel_path,
        title=title,
        content_hash=f"hash-{note_id}",
        type="active",
        tags=[],
        extra=extra,
        indexed_at=datetime.now(timezone.utc),
    )
    session.add(note)
    session.commit()
    session.refresh(note)

    # Write the vault file
    vault_file = tmp_path / rel_path
    vault_file.parent.mkdir(parents=True, exist_ok=True)
    vault_file.write_text(
        f'---\nid: {note_id}\ntitle: "{title}"\ntype: active\n'
        f"status: {status}\n---\nTask body for {title}.\n"
    )
    return note


class TestDistillCompletedTasks:
    @pytest.mark.asyncio
    async def test_skips_already_distilled_tasks(self, tmp_path, session):
        """A done task with existing provenance is not re-distilled."""
        note = _make_task(session, tmp_path, "t-already", "Already Done")

        # Record provenance so it looks already distilled
        session.add(
            AtomRawProvenance(
                atom_fk=note.id,
                gardener_version=GARDENER_VERSION,
                derived_note_id="some-atom",
            )
        )
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)
        mock = AsyncMock()
        gardener._run_claude_subprocess = mock  # type: ignore[method-assign]

        distilled, failed = await gardener._distill_completed_tasks()

        assert distilled == 0
        assert failed == 0
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_cancelled_tasks(self, tmp_path, session):
        """Cancelled tasks are not distilled."""
        _make_task(session, tmp_path, "t-cancel", "Cancelled Task", status="cancelled")

        gardener = Gardener(vault_root=tmp_path, session=session)
        mock = AsyncMock()
        gardener._run_claude_subprocess = mock  # type: ignore[method-assign]

        distilled, failed = await gardener._distill_completed_tasks()

        assert distilled == 0
        assert failed == 0
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_distills_done_task(self, tmp_path, session):
        """A done task without provenance triggers distillation."""
        note = _make_task(session, tmp_path, "t-done", "Completed Task")
        processed = tmp_path / "_processed"
        processed.mkdir(exist_ok=True)

        async def fake_claude(prompt: str) -> None:
            # Simulate Claude creating an atom note
            (processed / "lesson-learned.md").write_text(
                '---\nid: lesson-learned\ntitle: "Lesson Learned"\n'
                "type: atom\ntags: []\n---\nA useful lesson.\n"
            )

        gardener = Gardener(vault_root=tmp_path, session=session)
        gardener._run_claude_subprocess = fake_claude  # type: ignore[method-assign]

        distilled, failed = await gardener._distill_completed_tasks()

        assert distilled == 1
        assert failed == 0

        # Provenance should be recorded with the derived note id
        prov = session.exec(
            select(AtomRawProvenance).where(
                AtomRawProvenance.atom_fk == note.id,
            )
        ).all()
        assert len(prov) == 1
        assert prov[0].derived_note_id == "lesson-learned"
        assert prov[0].gardener_version == GARDENER_VERSION

    @pytest.mark.asyncio
    async def test_records_no_new_notes_provenance(self, tmp_path, session):
        """Even if no files are created, provenance is recorded."""
        note = _make_task(session, tmp_path, "t-routine", "Routine Task")
        processed = tmp_path / "_processed"
        processed.mkdir(exist_ok=True)

        async def fake_claude(prompt: str) -> None:
            pass  # Claude creates nothing

        gardener = Gardener(vault_root=tmp_path, session=session)
        gardener._run_claude_subprocess = fake_claude  # type: ignore[method-assign]

        distilled, failed = await gardener._distill_completed_tasks()

        assert distilled == 1
        assert failed == 0

        prov = session.exec(
            select(AtomRawProvenance).where(
                AtomRawProvenance.atom_fk == note.id,
            )
        ).all()
        assert len(prov) == 1
        assert prov[0].derived_note_id == "no-new-notes"

    @pytest.mark.asyncio
    async def test_skips_active_status_tasks(self, tmp_path, session):
        """Tasks with status='active' are not distilled."""
        _make_task(session, tmp_path, "t-active", "Active Task", status="active")

        gardener = Gardener(vault_root=tmp_path, session=session)
        mock = AsyncMock()
        gardener._run_claude_subprocess = mock  # type: ignore[method-assign]

        distilled, failed = await gardener._distill_completed_tasks()

        assert distilled == 0
        assert failed == 0
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_distill_failure_counted(self, tmp_path, session):
        """A failed distillation increments the failed counter."""
        _make_task(session, tmp_path, "t-fail", "Failing Task")

        async def exploding_claude(prompt: str) -> None:
            raise RuntimeError("claude crashed")

        gardener = Gardener(vault_root=tmp_path, session=session)
        gardener._run_claude_subprocess = exploding_claude  # type: ignore[method-assign]

        distilled, failed = await gardener._distill_completed_tasks()

        assert distilled == 0
        assert failed == 1
