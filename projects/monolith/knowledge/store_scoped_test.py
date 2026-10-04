"""Tests for scoped assertions, disputes, and provenance in the store."""

import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

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
    exact_query_tokens,
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


@pytest.mark.parametrize("state", ["open", "resolution_failed"])
def test_disputes_and_provenance_are_batched_by_note(session, state):
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
    session.add(Dispute(note_id="scoped", reason="contradictory evidence", state=state))
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


@pytest.mark.parametrize("state", ["open", "resolution_failed"])
def test_search_and_get_note_project_scoped_fields_with_real_session(session, state):
    test_now = datetime(2026, 9, 15, tzinfo=timezone.utc)
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
        observed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        review_after=datetime(2026, 11, 1, tzinfo=timezone.utc),
        review_policy="standard-90d/v1",
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
    session.add(Dispute(note_id="scoped", reason="contradictory evidence", state=state))
    session.commit()
    session.refresh(chunk)

    embedding = [0.0] * 1024
    with patch(
        "knowledge.store._rank_search_chunks",
        return_value=[(note.id, chunk.id, 0.9)],
    ) as rank:
        results = KnowledgeStore(session, now=test_now).search_notes_with_context(
            embedding
        )

    rank.assert_called_once_with(
        session,
        embedding,
        20,
        None,
        scope_filters=None,
        include_unscoped=False,
        exclude_invalidated=True,
        include_legacy=False,
        include_deployment_observations=False,
        query_text=None,
        include_history=False,
        now=ANY,
    )
    detail = KnowledgeStore(session, now=test_now).get_note_by_id("scoped")
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
        / "chart/migrations/20260926000000_personal_retrieval_audit.sql"
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


_OBSERVATION_PREDICATE = (
    "knowledge.notes.source IS NULL OR knowledge.notes.source != %(source_1)s"
)


def _compiled_rank_sql(**kwargs):
    kwargs.setdefault("now", datetime(2026, 10, 3, tzinfo=timezone.utc))
    session = MagicMock()
    session.execute.return_value.all.return_value = []
    _rank_search_chunks(
        session, [0.0] * 1024, 2, kwargs.pop("type_filter", None), **kwargs
    )
    return session.execute.call_args.args[0].compile(dialect=postgresql.dialect())


def _compiled_search_notes_sql(**kwargs):
    session = MagicMock()
    session.execute.return_value.all.return_value = []
    KnowledgeStore(session).search_notes([0.0] * 1024, limit=2, **kwargs)
    return session.execute.call_args.args[0].compile(dialect=postgresql.dialect())


@pytest.mark.parametrize(
    "compile_sql", [_compiled_rank_sql, _compiled_search_notes_sql]
)
def test_default_search_hides_deployment_observations_before_the_limit(compile_sql):
    compiled = compile_sql()
    sql = str(compiled)

    assert _OBSERVATION_PREDICATE in sql
    # NULL-source notes must survive: a bare != would drop them in SQL.
    assert sql.index("WHERE") < sql.index(_OBSERVATION_PREDICATE) < sql.index("LIMIT")
    assert "deployment-observation" in compiled.params.values()


@pytest.mark.parametrize(
    "compile_sql", [_compiled_rank_sql, _compiled_search_notes_sql]
)
def test_deployment_observation_opt_in_omits_the_source_predicate(compile_sql):
    compiled = compile_sql(include_deployment_observations=True)

    assert "knowledge.notes.source" not in str(compiled)
    assert "deployment-observation" not in compiled.params.values()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", ()),
        ("a semantic question", ()),
        ("#12 and #123", ("#12", "#123")),
        ("#12/#123", ("#12", "#123")),
        ("(`#12`), [projects/a_b/store.py].", ("#12", "projects/a_b/store.py")),
        (
            "'./bazel/ocaml/README.md'; bazel/ocaml/README.md!",
            ("bazel/ocaml/README.md",),
        ),
        ("pull/6043 issues/5250", ("pull/6043", "issues/5250")),
        ("https://github.com/o/r/pull/6043#12 file:projects/a.py mailto:a/b", ()),
        ("x=https://github.com/o/r/pull/6043 (https://a/b.md) <file:a/b.md>", ()),
        (
            "see a/b.py:12 a/b.md:1-3 (a/b.md:12) a/b.md: and a/c.md:",
            ("a/b.py", "a/b.md", "a/c.md"),
        ),
        (
            "What does projects/monolith/knowledge/store.py:300 do?",
            ("projects/monolith/knowledge/store.py",),
        ),
        ("a/b%20.md a/%/b.md", ()),
        ("foo#12 #12abc ##12", ()),
        ("`a/b.md` a/b.md.bak a/b.mdx", ("a/b.md", "a/b.md.bak", "a/b.mdx")),
    ],
)
def test_exact_query_tokens(query, expected):
    assert exact_query_tokens(query) == expected


def test_exact_query_token_bounds():
    assert exact_query_tokens(" ".join(f"#{i}" for i in range(20))) == tuple(
        f"#{i}" for i in range(8)
    )
    assert exact_query_tokens("a/" + "x" * 255 + " #12") == ("#12",)
    assert exact_query_tokens("#" + "1" * 256 + " a/b") == ("a/b",)
    assert exact_query_tokens("a/" + "x" * 254) == ("a/" + "x" * 254,)
    assert exact_query_tokens("a/" + "x" * 2054 + " #12") == ("#12",)


def test_exact_query_tokens_skip_overlong_words_quickly():
    start = time.perf_counter()
    assert exact_query_tokens("a" * 200_000) == ()
    assert exact_query_tokens("a." * 100_000 + " #12") == ("#12",)
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize("query", ["#12", "projects/a/store.py", "#12 a/b.py"])
def test_exact_provenance_subqueries_are_uncorrelated(query):
    """A correlated EXISTS rescans provenance for every note/chunk row."""
    sql = str(_compiled_rank_sql(query_text=query))

    assert "EXISTS" not in sql
    assert "atom_raw_provenance.derived_note_id = knowledge.notes.note_id" not in sql
    assert "atom_raw_provenance.atom_fk = knowledge.notes.id" not in sql
    assert "knowledge.note_entities.note_id = knowledge.notes.note_id" not in sql


@pytest.mark.parametrize(
    "query", [None, "", "ordinary semantic query", "https://o/r/#12"]
)
def test_no_exact_tokens_preserve_compiled_rank_sql(query):
    before = _compiled_rank_sql()
    after = _compiled_rank_sql(query_text=query)
    assert str(after) == str(before)
    assert after.params == before.params


def test_exact_rank_preserves_authorization_before_limit_and_binds_tokens():
    compiled = _compiled_rank_sql(
        query_text="#12 projects/a_b/store.py",
        scope_filters=("repo:owner/repo",),
        exclude_invalidated=True,
        type_filter="fact",
    )
    sql = str(compiled)
    where = sql[sql.index("WHERE") : sql.index("GROUP BY")]
    for predicate in (
        "knowledge.notes.deleted_at IS NULL",
        "knowledge.notes.scope IN",
        "knowledge.notes.type =",
        "knowledge.notes.verification_state IS NULL",
        "knowledge.notes.verification_state !=",
        _OBSERVATION_PREDICATE,
        "knowledge.notes.valid_until IS NULL",
    ):
        assert predicate in where
    assert "chunks ON knowledge.chunks.note_fk = knowledge.notes.id" in sql
    assert " OR bool_or(" in sql[sql.index("HAVING") : sql.index("ORDER BY")]
    order = sql[sql.index("ORDER BY") : sql.index("LIMIT")]
    assert order.index("bool_or") < order.index("CASE") < order.index("score DESC")
    assert order.rstrip().endswith("knowledge.notes.id")
    assert "#12" not in sql and "projects/a_b/store.py" not in sql
    assert "12" in compiled.params.values()
    assert any(
        "projects/a_b/store\\.py" in str(value) for value in compiled.params.values()
    )
