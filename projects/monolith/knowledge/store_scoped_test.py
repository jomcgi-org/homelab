"""Tests for scoped assertions, disputes, and provenance in the store."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from auth.api import Authority, Principal, PrincipalKind
from sqlalchemy.dialects import postgresql
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge.frontmatter import ParsedFrontmatter
from knowledge.indexing import index_note_from_raw
from knowledge.models import (
    AtomRawProvenance,
    Chunk,
    Dispute,
    Note,
    PersonalRetrievalAudit,
    RawInput,
)
from knowledge.retrieval_policy import (
    RetrievalAuditError,
    audit_personal_retrieval,
    authorize_retrieval,
)
from knowledge.store import (
    KnowledgeStore,
    _rank_search_chunks,
    _resolve_edge_targets,
    open_dispute_note_ids,
    provenance_for_notes,
)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge.db'}")
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


def _upsert(store: KnowledgeStore, metadata: ParsedFrontmatter) -> None:
    store.upsert_note(
        note_id="scoped",
        path="scoped.md",
        content_hash="hash",
        title="Scoped",
        metadata=metadata,
        chunks=[{"index": 0, "section_header": "", "text": "body text"}],
        vectors=[[0.0] * 1024],
        links=[],
        content="body text",
    )


def test_upsert_sets_scoped_columns(session):
    store = KnowledgeStore(session)
    valid_from = datetime(2026, 9, 1, tzinfo=timezone.utc)
    valid_until = datetime(2026, 10, 1, tzinfo=timezone.utc)
    observed_at = datetime(2026, 9, 2, tzinfo=timezone.utc)

    _upsert(
        store,
        ParsedFrontmatter(
            scope="org:factory",
            verification_state="verified",
            confidence=0.9,
            valid_from=valid_from,
            valid_until=valid_until,
            observed_at=observed_at,
        ),
    )

    note = session.exec(select(Note).where(Note.note_id == "scoped")).one()
    assert note.scope == "org:factory"
    assert note.verification_state == "verified"
    assert note.confidence == 0.9
    assert note.valid_from.replace(tzinfo=timezone.utc) == valid_from
    assert note.valid_until.replace(tzinfo=timezone.utc) == valid_until
    assert note.observed_at.replace(tzinfo=timezone.utc) == observed_at


@pytest.mark.asyncio
async def test_reindex_without_scoped_keys_preserves_existing_values(session):
    store = KnowledgeStore(session)
    observed_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    _upsert(
        store,
        ParsedFrontmatter(
            scope="personal:alice",
            verification_state="verified",
            confidence=0.8,
            observed_at=observed_at,
        ),
    )

    embedder = MagicMock()

    async def embed_batch(texts):
        return [[0.0] * 1024 for _ in texts]

    embedder.embed_batch = embed_batch
    await index_note_from_raw(
        store,
        embedder,
        note_id="scoped",
        rel_path="scoped.md",
        raw="---\nid: scoped\ntitle: Scoped\n---\n\nnew body\n",
    )

    note = session.exec(select(Note).where(Note.note_id == "scoped")).one()
    assert note.scope == "personal:alice"
    assert note.verification_state == "verified"
    assert note.confidence == 0.8
    assert note.observed_at.replace(tzinfo=timezone.utc) == observed_at


def test_disputes_and_provenance_are_batched_by_note(session):
    note = Note(
        note_id="scoped",
        path="scoped.md",
        title="Scoped",
        content_hash="hash",
    )
    raw = RawInput(
        raw_id="raw-1",
        path="raws/raw-1.md",
        source="collector",
        content_hash="raw-1",
    )
    session.add(note)
    session.add(raw)
    session.commit()
    session.refresh(note)
    session.refresh(raw)
    session.add(
        AtomRawProvenance(
            raw_fk=raw.id,
            derived_note_id="scoped",
            gardener_version="current-version",
        )
    )
    session.add(
        AtomRawProvenance(
            atom_fk=note.id,
            gardener_version="legacy-version",
        )
    )
    session.add(
        AtomRawProvenance(
            atom_fk=note.id,
            raw_fk=raw.id,
            derived_note_id="failed",
            gardener_version="sentinel-version",
        )
    )
    session.add(Dispute(note_id="scoped", reason="contradictory evidence"))
    session.add(Dispute(note_id="closed", reason="resolved", state="confirmed"))
    session.commit()

    assert open_dispute_note_ids(session, ["scoped", "closed", "missing"]) == {"scoped"}
    assert provenance_for_notes(session, ["scoped"]) == {
        "scoped": [
            {
                "raw_id": "raw-1",
                "source": "collector",
                "gardener_version": "current-version",
            },
            {
                "raw_id": None,
                "source": None,
                "gardener_version": "legacy-version",
            },
        ]
    }


def test_search_and_get_note_project_scoped_fields_with_real_session(session):
    note = Note(
        note_id="scoped",
        path="scoped.md",
        title="Scoped",
        content_hash="hash",
        content="supported claim",
        type="fact",
        tags=["test"],
        scope="repo:owner/repo",
        verification_state="verified",
        confidence=0.7,
        valid_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    raw = RawInput(
        raw_id="raw-1",
        path="raws/raw-1.md",
        source="collector",
        content_hash="raw-1",
    )
    session.add(note)
    session.add(raw)
    session.commit()
    session.refresh(note)
    session.refresh(raw)
    chunk = Chunk(
        note_fk=note.id,
        chunk_index=0,
        section_header="## Evidence",
        chunk_text="supported claim",
        embedding=[0.0] * 1024,
    )
    session.add(chunk)
    session.add(
        AtomRawProvenance(
            raw_fk=raw.id,
            derived_note_id="scoped",
            gardener_version="v1",
        )
    )
    session.add(Dispute(note_id="scoped", reason="contradictory evidence"))
    session.commit()
    session.refresh(chunk)

    embedding = [0.0] * 1024
    with patch(
        "knowledge.store._rank_search_chunks",
        return_value=[(note.id, chunk.id, 0.9)],
    ) as rank:
        results = KnowledgeStore(session).search_notes_with_context(embedding)

    rank.assert_called_once_with(
        session,
        embedding,
        20,
        None,
        scope_filters=None,
        include_unscoped=False,
        exclude_invalidated=False,
        include_legacy=False,
    )
    detail = KnowledgeStore(session).get_note_by_id("scoped")
    assert detail is not None
    for result in (results[0], detail):
        assert result["scope"] == "repo:owner/repo"
        assert result["verification_state"] == "verified"
        assert result["disputed"] is True
        assert result["provenance"] == [
            {"raw_id": "raw-1", "source": "collector", "gardener_version": "v1"}
        ]


def test_search_scope_allow_list_is_applied_before_ranking():
    session = MagicMock()
    session.execute.return_value.all.return_value = []

    _rank_search_chunks(
        session,
        [0.0] * 1024,
        2,
        None,
        scope_filters=("repo:owner/repo", "org:owner"),
        include_unscoped=True,
    )

    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "knowledge.notes.scope IN" in sql
    assert "knowledge.notes.scope IS NULL" in sql
    assert sql.index("WHERE") < sql.index("LIMIT")
    scope_params = [
        value for value in compiled.params.values() if isinstance(value, (list, tuple))
    ]
    assert any(set(value) == {"repo:owner/repo", "org:owner"} for value in scope_params)


def test_empty_scope_allow_list_does_not_fall_back_to_unrestricted_search():
    session = MagicMock()
    session.execute.return_value.all.return_value = []

    _rank_search_chunks(
        session,
        [0.0] * 1024,
        2,
        None,
        scope_filters=(),
    )

    sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "false" in sql


def test_edge_resolution_uses_search_allow_list(session):
    allowed = Note(
        note_id="allowed-target",
        path="allowed.md",
        title="Allowed",
        content_hash="allowed",
        scope="repo:owner/repo",
    )
    other_repo = Note(
        note_id="other-target",
        path="other.md",
        title="Other",
        content_hash="other",
        scope="repo:other/repo",
    )
    personal = Note(
        note_id="personal-target",
        path="personal.md",
        title="Personal",
        content_hash="personal",
        scope="personal:alice",
    )
    legacy = Note(
        note_id="legacy-target",
        path="legacy.md",
        title="Legacy",
        content_hash="legacy",
        scope=None,
    )
    session.add_all([allowed, other_repo, personal, legacy])
    session.commit()
    rows = [
        SimpleNamespace(kind="edge", target_id=note.note_id)
        for note in (allowed, other_repo, personal, legacy)
    ]

    defaults = _resolve_edge_targets(session, rows, scope_filters=("repo:owner/repo",))
    opted_in = _resolve_edge_targets(
        session,
        rows,
        scope_filters=("repo:owner/repo", "personal:alice"),
        include_unscoped=True,
    )

    assert defaults == {"allowed-target"}
    assert opted_in == {"allowed-target", "personal-target", "legacy-target"}


def test_personal_opt_in_persists_one_bounded_attribution_row(session):
    principal = Principal(
        subject="alice",
        actor=("operator",),
        scope=("repo:owner/repo", "personal:alice"),
        groups=(),
        email="alice@example.com",
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )
    authorization = authorize_retrieval(principal, include_personal=True)

    audit_personal_retrieval(session, principal, authorization, entrypoint="mcp")

    rows = session.exec(select(PersonalRetrievalAudit)).all()
    assert len(rows) == 1
    assert rows[0].principal_subject == "alice"
    assert rows[0].principal_actor == '["operator"]'
    assert rows[0].personal_scope == "personal:alice"
    assert rows[0].entrypoint == "mcp"
    assert not hasattr(rows[0], "query")
    assert not hasattr(rows[0], "results")


def test_personal_audit_rejects_oversized_attribution_before_insert():
    subject = "a" * 513
    principal = Principal(
        subject=subject,
        actor=(),
        scope=(f"personal:{subject}",),
        groups=(),
        email=None,
        kind=PrincipalKind.WORKLOAD,
        authority=Authority.STANDING,
    )
    authorization = authorize_retrieval(principal, include_personal=True)
    session = MagicMock()

    with pytest.raises(RetrievalAuditError, match="exceeds audit bounds"):
        audit_personal_retrieval(session, principal, authorization, entrypoint="mcp")

    session.execute.assert_not_called()
    session.commit.assert_not_called()


def test_search_rechecks_scope_while_hydrating_ranked_notes(session):
    other_repo = Note(
        note_id="scope-changed",
        path="scope-changed.md",
        title="Scope changed",
        content_hash="scope-changed",
        scope="repo:other/repo",
    )
    session.add(other_repo)
    session.commit()

    with patch(
        "knowledge.store._rank_search_chunks",
        return_value=[(other_repo.id, 999, 0.9)],
    ):
        results = KnowledgeStore(session).search_notes_with_context(
            [0.0] * 1024,
            scope_filters=("repo:owner/repo",),
        )

    assert results == []


def test_personal_audit_migration_enforces_retention_and_least_privilege():
    migration = (
        Path(__file__).parents[1]
        / "chart/migrations/20260923020000_personal_retrieval_audit.sql"
    ).read_text()

    assert "SECURITY DEFINER" in migration
    assert "SET search_path = pg_catalog" in migration
    assert "AFTER INSERT ON knowledge.personal_retrieval_audit" in migration
    assert "FOR EACH STATEMENT" in migration
    assert "created_at < pg_catalog.now() - INTERVAL '90 days'" in migration
    assert "ON TABLE knowledge.personal_retrieval_audit" in migration
    assert "GRANT INSERT (" in migration
    assert ") ON TABLE knowledge.personal_retrieval_audit" in migration
    assert "GRANT SELECT" not in migration
    assert "GRANT DELETE" not in migration
