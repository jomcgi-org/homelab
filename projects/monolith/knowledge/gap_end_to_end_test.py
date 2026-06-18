"""End-to-end integration test for the gap lifecycle.

Exercises all components in one flow:
  discover_gaps             → creates Gap row + stub file
  mock classifier           → simulates Claude editing stub frontmatter
  _project_gap_frontmatter  → projects frontmatter → DB (gap_class, state, version)
  list_review_queue         → surfaces the classified gap
  answer_gap                → indexes atom fileless + commits Gap

The only mock is the classifier itself (we don't spawn claude subprocesses
in tests). Everything else uses real SQLite + real filesystem.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.frontmatter import parse as parse_frontmatter
from knowledge.gap_stubs import RESEARCHING_DIR, parse_stub_frontmatter
from knowledge.gaps import (
    answer_gap,
    discover_gaps,
    list_review_queue,
)
from knowledge.models import Gap, Note, NoteLink
from knowledge.reconciler import _project_gap_frontmatter


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with the knowledge.* schema stripped.

    Mirrors the pattern in ``gap_lifecycle_test.py`` so the test runs
    without a real Postgres.
    """
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
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _classifier_mock(stub: Path, gap_class: str, *, status: str) -> None:
    """Simulate Claude editing a stub's frontmatter via the Edit tool.

    Reads the stub, updates gap_class + status + classifier_version +
    classified_at, writes back. The classifier prompt instructs Claude to
    set ``status: in_review`` for ``internal``/``hybrid`` gaps so the
    review queue picks them up — mirror that here rather than relying on
    a downstream auto-transition that the reconciler does not perform.
    """
    text = stub.read_text()
    parts = text.split("---\n", 2)
    fm = yaml.safe_load(parts[1])
    fm.update(
        {
            "gap_class": gap_class,
            "status": status,
            "classifier_version": "opus-4-7@v1",
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    new_fm = yaml.dump(fm, default_flow_style=False, sort_keys=False)
    body = parts[2] if len(parts) >= 3 else ""
    stub.write_text(f"---\n{new_fm}---\n{body}")


@pytest.mark.asyncio
async def test_full_gap_cycle(session: Session, tmp_path: Path) -> None:
    """Drive a single gap through the full lifecycle and assert the
    terminal state: atom indexed fileless, Gap committed."""
    # Step 1: Seed a source note referencing an unresolved [[wikilink]].
    source = Note(
        note_id="source",
        path="source.md",
        title="Source Note",
        content_hash="abc",
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    session.add(
        NoteLink(
            src_note_fk=source.id,
            target_id="linkerd-mtls",
            kind="link",
            edge_type=None,
        )
    )
    session.commit()

    # Step 2: discover_gaps — creates Gap row + stub file.
    created = discover_gaps(session, tmp_path)
    assert created == 1

    gap = session.execute(select(Gap).where(Gap.term == "linkerd-mtls")).scalar_one()
    assert gap.state == "discovered"
    assert gap.gap_class is None
    assert gap.note_id == "linkerd-mtls"

    stub_path = tmp_path / RESEARCHING_DIR / "linkerd-mtls.md"
    assert stub_path.is_file()

    # Step 3: Mock the classifier — simulate Claude marking the gap
    # 'internal' and routing it directly to in_review (the prompt
    # instructs Claude to do this for internal/hybrid classes).
    _classifier_mock(stub_path, "internal", status="in_review")

    stub_meta = parse_stub_frontmatter(stub_path)
    assert stub_meta["gap_class"] == "internal"
    assert stub_meta["status"] == "in_review"

    # Step 4: Reconciler projects frontmatter → DB. We invoke the
    # projection helper directly rather than spinning up the full
    # Reconciler (which requires a KnowledgeStore + embedder); the unit
    # under test for this composition is the projection itself.
    meta, _ = parse_frontmatter(stub_path.read_text())
    _project_gap_frontmatter(session, "linkerd-mtls", meta)

    session.refresh(gap)
    assert gap.gap_class == "internal"
    assert gap.state == "in_review"
    assert gap.pipeline_version == "opus-4-7@v1"
    assert gap.classified_at is not None

    # Step 5: list_review_queue should return the gap.
    queue = list_review_queue(session)
    assert len(queue) == 1
    assert queue[0]["term"] == "linkerd-mtls"
    assert queue[0]["gap_class"] == "internal"

    # Step 6: User answers the gap via answer_gap (fileless). The shared
    # atom build+index core is mocked so this stays a DB-only integration
    # test; the contract under test here is the gap-state transition.
    index = AsyncMock(return_value="linkerd-mtls")
    with patch("knowledge.mcp._index_atom", index):
        result = await answer_gap(
            session,
            gap.id,
            "Linkerd mTLS provides mutual TLS authentication between meshed services.",
        )

    assert result == {"gap_id": gap.id, "note_id": "linkerd-mtls"}

    # The atom was built with the personal/private framing.
    index.assert_awaited_once()
    kwargs = index.call_args.kwargs
    assert kwargs["source_tier"] == "personal"
    assert kwargs["visibility"] == "private"
    assert kwargs["title"] == "linkerd-mtls"

    # Terminal assertion: Gap is committed with note_id + resolved_at set.
    session.refresh(gap)
    assert gap.state == "committed"
    assert gap.note_id == "linkerd-mtls"
    assert gap.answer is not None
    assert gap.resolved_at is not None
