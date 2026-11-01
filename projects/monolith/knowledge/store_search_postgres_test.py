"""Knowledge store search contracts against PostgreSQL 16 with pgvector."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlmodel import Session, create_engine, select

from knowledge.frontmatter import ParsedFrontmatter
from knowledge.links import Link
from knowledge.models import Note
from knowledge.store import KnowledgeStore

_NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
_QUERY = [1.0] + [0.0] * 1023
_OTHER = [0.8, 0.6] + [0.0] * 1022


@pytest.fixture
def pg_session(pg):
    engine = create_engine(pg.url)
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection) as session:
            yield session
        transaction.rollback()
    engine.dispose()


def _meta(**fields):
    fields.setdefault("observed_at", _NOW - timedelta(hours=1))
    fields.setdefault("scope", "repo:jomcgi-org/homelab")
    return ParsedFrontmatter(**fields)


def _upsert(
    store,
    *,
    note_id="a-id",
    path="a.md",
    content_hash="h1",
    title="A",
    metadata=None,
    n_chunks=1,
    links=None,
    vectors=None,
):
    store.upsert_note(
        note_id=note_id,
        path=path,
        content_hash=content_hash,
        title=title,
        metadata=metadata or _meta(title=title, verification_state="verified"),
        chunks=[
            {"index": i, "section_header": f"H{i}", "text": f"chunk {i} " * 20}
            for i in range(n_chunks)
        ],
        vectors=vectors or [_QUERY if i == 0 else _OTHER for i in range(n_chunks)],
        links=links or [],
    )


class TestSearchNotes:
    """search_notes requires pgvector cosine_distance (Postgres only)."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_session):
        self.session = pg_session
        self.store = KnowledgeStore(session=pg_session, now=_NOW)

    def test_empty_returns_empty_list(self):
        result = self.store.search_notes(query_embedding=_QUERY)
        assert result == []

    def test_returns_matching_notes_ranked_by_score(self):
        # Insert two notes with distinct nonzero embeddings.
        _upsert(self.store, note_id="n1", path="a.md", title="Alpha", n_chunks=1)
        _upsert(
            self.store,
            note_id="n2",
            path="b.md",
            title="Beta",
            n_chunks=1,
            vectors=[_OTHER],
        )

        # Query with the same embedding as note n1's chunk.
        results = self.store.search_notes(query_embedding=_QUERY)
        assert len(results) == 2
        # Both results should have the required keys.
        assert set(results[0].keys()) == {"note_id", "title", "path", "score"}
        # The exact vector match ranks ahead of the lower cosine similarity.
        assert results[0]["note_id"] == "n1"

    def test_limit_restricts_result_count(self):
        for i in range(5):
            _upsert(
                self.store,
                note_id=f"n{i}",
                path=f"{i}.md",
                title=f"Note {i}",
                n_chunks=1,
            )
        results = self.store.search_notes(query_embedding=_QUERY, limit=2)
        assert len(results) == 2

    def test_exclude_ids_filters_notes(self):
        _upsert(self.store, note_id="n1", path="a.md", title="Alpha", n_chunks=1)
        _upsert(self.store, note_id="n2", path="b.md", title="Beta", n_chunks=1)

        results = self.store.search_notes(query_embedding=_QUERY, exclude_ids=["n1"])
        note_ids = [r["note_id"] for r in results]
        assert "n1" not in note_ids
        assert "n2" in note_ids

    def test_score_is_between_zero_and_one(self):
        _upsert(self.store, note_id="n1", path="a.md", title="Alpha", n_chunks=1)
        results = self.store.search_notes(query_embedding=_QUERY)
        assert len(results) == 1
        assert 0.0 <= results[0]["score"] <= 1.0

    def test_best_chunk_score_used_for_multi_chunk_note(self):
        # A note with two chunks uses the best chunk score.
        _upsert(self.store, note_id="n1", path="a.md", title="Alpha", n_chunks=2)
        results = self.store.search_notes(query_embedding=_QUERY)
        assert len(results) == 1
        # Chunk 0 matches the query exactly; chunk 1 has similarity 0.8.
        assert results[0]["score"] == pytest.approx(1.0, abs=1e-6)


class TestSearchNotesWithContext:
    """search_notes_with_context requires pgvector cosine_distance (Postgres only)."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_session):
        self.session = pg_session
        self.now = _NOW
        self.store = KnowledgeStore(session=pg_session, now=self.now)

    def _meta(self, **fields):
        fields.setdefault("observed_at", self.now - timedelta(hours=1))
        return _meta(**fields)

    def test_returns_type_tags_snippet_and_section(self):
        # Insert a note with a distinctive chunk under "## Architecture".
        distinctive = "transformer replaces recurrence entirely " * 4
        self.store.upsert_note(
            note_id="n1",
            path="attention.md",
            content_hash="h1",
            title="Attention",
            metadata=self._meta(
                title="Attention",
                type="paper",
                tags=["ml", "transformers"],
                verification_state="verified",
            ),
            chunks=[
                {
                    "index": 0,
                    "section_header": "## Architecture",
                    "text": distinctive,
                }
            ],
            vectors=[_QUERY],
            links=[],
        )

        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        assert len(results) == 1
        row = results[0]
        assert set(row.keys()) == {
            "note_id",
            "title",
            "path",
            "type",
            "tags",
            "score",
            "section",
            "snippet",
            "edges",
            "scope",
            "verification_state",
            "confidence",
            "valid_from",
            "valid_until",
            "observed_at",
            "review_after",
            "review_policy",
            "last_reviewed_at",
            "freshness",
            "requires_authoritative_observation",
            "disputed",
            "provenance",
            "entities",
        }
        assert row["note_id"] == "n1"
        assert row["title"] == "Attention"
        assert row["path"] == "attention.md"
        assert row["type"] == "paper"
        assert row["tags"] == ["ml", "transformers"]
        assert row["section"] == "## Architecture"
        assert distinctive in row["snippet"]

    def test_scope_filter_isolates_personal_and_receipt_notes(self):
        vector = _QUERY
        scopes = {
            "operator-a": "personal:operator-a:factory-receipt:7",
            "operator-b": "personal:operator-b:factory-receipt:7",
            "receipt": "session:factory-receipt:7",
        }
        for note_id, scope in scopes.items():
            self.store.upsert_note(
                note_id=note_id,
                path=f"{note_id}.md",
                content_hash=note_id,
                title=note_id,
                metadata=self._meta(
                    title=note_id,
                    type="fact",
                    scope=scope,
                    verification_state="verified",
                ),
                chunks=[
                    {"index": 0, "section_header": "", "text": (note_id + " ") * 20}
                ],
                vectors=[vector],
                links=[],
            )

        for note_id, scope in scopes.items():
            results = self.store.search_notes_with_context(
                vector,
                scope_filter=scope,
            )
            assert [row["note_id"] for row in results] == [note_id]

    def test_best_chunk_snippet_used_for_multi_chunk_note(self):
        # One note with two chunks whose embeddings are far apart. The
        # DISTINCT ON query must pick the closest chunk, even when it is second.
        other_text = "orthogonal chunk about attention heads " * 4
        ones_text = "ones chunk about positional encoding " * 4
        self.store.upsert_note(
            note_id="n1",
            path="multi.md",
            content_hash="h1",
            title="Multi",
            metadata=self._meta(
                title="Multi",
                type="paper",
                tags=["ml"],
                verification_state="verified",
            ),
            chunks=[
                {
                    "index": 0,
                    "section_header": "## Other",
                    "text": other_text,
                },
                {
                    "index": 1,
                    "section_header": "## Ones",
                    "text": ones_text,
                },
            ],
            vectors=[[1.0, -1.0] + [0.0] * 1022, [1.0] * 1024],
            links=[],
        )

        # Query matches the all-ones chunk exactly.
        results = self.store.search_notes_with_context(query_embedding=[1.0] * 1024)
        assert len(results) == 1
        row = results[0]
        assert row["section"] == "## Ones"
        assert ones_text in row["snippet"]
        assert other_text not in row["snippet"]

    def test_hydration_queries_project_only_response_columns(self):
        self.store.upsert_note(
            note_id="projected",
            path="projected.md",
            content_hash="projected-hash",
            title="Projected",
            metadata=self._meta(
                title="Projected",
                type="fact",
                tags=["query"],
                verification_state="verified",
            ),
            chunks=[
                {
                    "index": 0,
                    "section_header": "## Projection",
                    "text": "project only the columns used by the response " * 4,
                }
            ],
            vectors=[_QUERY],
            links=[],
            content="full note content must not be hydrated",
        )
        statements = []

        def capture_sql(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        bind = self.session.get_bind()
        event.listen(bind, "before_cursor_execute", capture_sql)
        try:
            without_embeddings = self.store.search_notes_with_context(
                query_embedding=_QUERY
            )
            first_call_statements = list(statements)
            statements.clear()
            with_embeddings = self.store.search_notes_with_context(
                query_embedding=_QUERY,
                include_embeddings=True,
            )
            second_call_statements = list(statements)
        finally:
            event.remove(bind, "before_cursor_execute", capture_sql)

        def hydration_sql(captured, table, required_column):
            return next(
                statement
                for statement in captured
                if f"FROM knowledge.{table}" in statement
                and required_column in statement
                and f"knowledge.{table}.id IN" in statement
            )

        note_sql = hydration_sql(first_call_statements, "notes", "notes.title")
        chunk_sql = hydration_sql(
            first_call_statements, "chunks", "chunks.section_header"
        )
        chunk_sql_with_embedding = hydration_sql(
            second_call_statements, "chunks", "chunks.section_header"
        )
        assert "notes.content" not in note_sql
        assert "notes.content_hash" not in note_sql
        assert "notes.extra" not in note_sql
        assert "chunks.embedding" not in chunk_sql
        assert "chunks.embedding" in chunk_sql_with_embedding
        assert without_embeddings[0]["snippet"].startswith("project only")
        assert "embedding" not in without_embeddings[0]
        assert with_embeddings[0]["embedding"] == _QUERY

    def test_filters_by_type(self):
        _upsert(
            self.store,
            note_id="n1",
            path="a.md",
            title="Paper",
            metadata=self._meta(
                title="Paper", type="paper", verification_state="verified"
            ),
            n_chunks=1,
        )
        _upsert(
            self.store,
            note_id="n2",
            path="b.md",
            title="Journal",
            metadata=self._meta(
                title="Journal", type="journal", verification_state="verified"
            ),
            n_chunks=1,
        )
        results = self.store.search_notes_with_context(
            query_embedding=_QUERY, type_filter="paper"
        )
        assert len(results) == 1
        assert results[0]["note_id"] == "n1"
        assert results[0]["type"] == "paper"

    def test_can_exclude_invalidated_notes_for_dedupe(self):
        content = "A sufficiently long behavioral note body for stable vector ranking."
        for note_id in ("current", "expired", "invalidated", "future-expiry"):
            self.store.upsert_note(
                note_id=note_id,
                path=f"{note_id}.md",
                content_hash=note_id,
                title=note_id,
                metadata=self._meta(
                    title=note_id, type="fact", verification_state="verified"
                ),
                chunks=[{"index": 0, "section_header": "", "text": content}],
                vectors=[_QUERY],
                links=[],
            )
        expired = self.session.exec(select(Note).where(Note.note_id == "expired")).one()
        invalidated = self.session.exec(
            select(Note).where(Note.note_id == "invalidated")
        ).one()
        future_expiry = self.session.exec(
            select(Note).where(Note.note_id == "future-expiry")
        ).one()
        now = self.now
        expired.valid_until = now - timedelta(days=30)
        future_expiry.valid_until = now + timedelta(days=365)
        invalidated.verification_state = "invalidated"
        self.session.add_all([expired, invalidated, future_expiry])
        self.session.commit()

        public_results = self.store.search_notes_with_context(
            _QUERY, include_history=True
        )
        dedupe_results = self.store.search_notes_with_context(
            _QUERY, exclude_invalidated=True
        )

        assert {row["note_id"] for row in public_results} == {
            "current",
            "expired",
            "invalidated",
            "future-expiry",
        }
        assert {row["note_id"] for row in dedupe_results} == {
            "current",
            "future-expiry",
        }

    @pytest.mark.parametrize("excluded", ["invalidated", "expired"])
    def test_validity_filter_runs_before_limit(self, excluded):
        content = (
            "A long enough note body to avoid the short chunk ranking penalty. " * 2
        )
        query = [1.0] + [0.0] * 1023
        for note_id, vector in (
            ("excluded-top", query),
            ("disputed-lower", [0.8, 0.6] + [0.0] * 1022),
        ):
            self.store.upsert_note(
                note_id=note_id,
                path=f"{note_id}.md",
                content_hash=note_id,
                title=note_id,
                metadata=self._meta(
                    title=note_id,
                    type="fact",
                    verification_state=(
                        "disputed" if note_id == "disputed-lower" else "verified"
                    ),
                ),
                chunks=[{"index": 0, "section_header": "", "text": content}],
                vectors=[vector],
                links=[],
            )
        top = self.session.exec(
            select(Note).where(Note.note_id == "excluded-top")
        ).one()
        if excluded == "invalidated":
            top.verification_state = "invalidated"
        else:
            top.valid_until = self.now - timedelta(days=30)
        self.session.add(top)
        self.session.commit()

        filtered = self.store.search_notes_with_context(
            query, limit=1, exclude_invalidated=True
        )
        unfiltered = self.store.search_notes_with_context(query, limit=1)

        assert [row["note_id"] for row in filtered] == ["disputed-lower"]
        assert [row["note_id"] for row in unfiltered] == ["excluded-top"]

    def test_legacy_filter_runs_before_limit_and_can_be_opted_out(self):
        content = (
            "A long enough note body to avoid the short chunk ranking penalty. " * 2
        )
        query = [1.0] + [0.0] * 1023
        candidates = (
            ("legacy-top", "legacy", [1.0] + [0.0] * 1023),
            ("verified-lower", "verified", [0.8, 0.6] + [0.0] * 1022),
        )
        for note_id, state, vector in candidates:
            self.store.upsert_note(
                note_id=note_id,
                path=f"{note_id}.md",
                content_hash=note_id,
                title=note_id,
                metadata=self._meta(
                    title=note_id,
                    type="fact",
                    verification_state=state,
                ),
                chunks=[{"index": 0, "section_header": "", "text": content}],
                vectors=[vector],
                links=[],
            )

        default_results = self.store.search_notes_with_context(query, limit=1)
        archive_results = self.store.search_notes_with_context(
            query, limit=2, include_legacy=True
        )

        assert [row["note_id"] for row in default_results] == ["verified-lower"]
        assert [row["note_id"] for row in archive_results] == [
            "legacy-top",
            "verified-lower",
        ]

    def test_legacy_opt_in_does_not_bypass_other_filters(self):
        content = (
            "A long enough note body to avoid the short chunk ranking penalty. " * 2
        )
        for note_id, state in (
            ("current", "verified"),
            ("legacy-current", "legacy"),
            ("legacy-invalidated", "invalidated"),
            ("legacy-expired", "legacy"),
            ("legacy-deleted", "legacy"),
            ("legacy-other-scope", "legacy"),
            ("personal-private", "verified"),
        ):
            self.store.upsert_note(
                note_id=note_id,
                path=f"{note_id}.md",
                content_hash=note_id,
                title=note_id,
                metadata=self._meta(
                    title=note_id,
                    type="fact",
                    scope=(
                        "repo:other/project"
                        if note_id == "legacy-other-scope"
                        else (
                            "personal:operator"
                            if note_id == "personal-private"
                            else "repo:jomcgi-org/homelab"
                        )
                    ),
                    verification_state=state,
                ),
                chunks=[{"index": 0, "section_header": "", "text": content}],
                vectors=[[1.0] + [0.0] * 1023],
                links=[],
            )
        expired = self.session.exec(
            select(Note).where(Note.note_id == "legacy-expired")
        ).one()
        deleted = self.session.exec(
            select(Note).where(Note.note_id == "legacy-deleted")
        ).one()
        expired.valid_until = datetime(2026, 9, 1, tzinfo=timezone.utc)
        deleted.deleted_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.session.add(expired)
        self.session.add(deleted)
        self.session.commit()

        filtered = self.store.search_notes_with_context(
            [1.0] + [0.0] * 1023,
            scope_filter="repo:jomcgi-org/homelab",
            exclude_invalidated=True,
            include_legacy=True,
        )
        validity_unfiltered = self.store.search_notes_with_context(
            [1.0] + [0.0] * 1023,
            scope_filter="repo:jomcgi-org/homelab",
            include_legacy=True,
        )

        assert {row["note_id"] for row in filtered} == {
            "current",
            "legacy-current",
        }
        assert {row["note_id"] for row in validity_unfiltered} == {
            "current",
            "legacy-current",
            "legacy-invalidated",
            "legacy-expired",
        }

    def test_empty_db_returns_empty_list(self):
        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        assert results == []

    def test_due_notes_require_explicit_history(self):
        for note_id, observed_at in (
            ("current", self.now - timedelta(hours=1)),
            ("due", self.now - timedelta(days=90)),
        ):
            _upsert(
                self.store,
                note_id=note_id,
                path=f"{note_id}.md",
                metadata=self._meta(
                    title=note_id,
                    observed_at=observed_at,
                    verification_state="verified",
                ),
            )
        current = self.store.search_notes_with_context(_QUERY)
        history = self.store.search_notes_with_context(_QUERY, include_history=True)
        assert {row["note_id"] for row in current} == {"current"}
        assert {row["note_id"] for row in history} == {"current", "due"}
        assert {row["note_id"]: row["freshness"] for row in history}["due"] == "due"


class TestSearchNotesWithContextEdges:
    """Verify the ``edges`` key in PostgreSQL context search results."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_session):
        self.session = pg_session
        self.store = KnowledgeStore(session=pg_session, now=_NOW)

    def test_search_result_includes_edges_key(self):
        """Every search result must have an 'edges' key."""
        _upsert(self.store, note_id="n1", path="a.md", title="Alpha", n_chunks=1)
        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        assert len(results) == 1
        assert "edges" in results[0]

    def test_edges_is_list(self):
        """The 'edges' value is always a list (not None, not dict)."""
        _upsert(self.store, note_id="n1", path="a.md", title="Alpha", n_chunks=1)
        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        assert isinstance(results[0]["edges"], list)

    def test_empty_edges_when_note_has_no_links(self):
        """A note with no links or edges produces edges=[]."""
        _upsert(
            self.store,
            note_id="n1",
            path="a.md",
            title="Alpha",
            n_chunks=1,
            links=[],
        )
        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        assert results[0]["edges"] == []

    def test_edges_contain_wikilink_data(self):
        """Wikilinks appear in edges with kind='link' and edge_type=None."""
        _upsert(
            self.store,
            note_id="n1",
            path="a.md",
            title="Alpha",
            n_chunks=1,
            links=[Link(target="n2", display="Beta")],
        )
        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        assert len(results) == 1
        edges = results[0]["edges"]
        assert len(edges) == 1
        assert edges[0]["target_id"] == "n2"
        assert edges[0]["target_title"] == "Beta"
        assert edges[0]["kind"] == "link"
        assert edges[0]["edge_type"] is None

    def test_edges_contain_frontmatter_edge_data(self):
        """Typed frontmatter edges appear in edges with kind='edge' and edge_type set."""
        _upsert(
            self.store,
            note_id="n1",
            path="a.md",
            title="Alpha",
            n_chunks=1,
            metadata=_meta(
                title="Alpha",
                edges={"refines": ["parent-id"]},
                verification_state="verified",
            ),
        )
        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        assert len(results) == 1
        edges = results[0]["edges"]
        assert len(edges) == 1
        assert edges[0]["target_id"] == "parent-id"
        assert edges[0]["kind"] == "edge"
        assert edges[0]["edge_type"] == "refines"

    def test_edges_dict_keys_for_link_kind(self):
        """Wikilink (kind='link') edge dicts have exactly 4 keys."""
        _upsert(
            self.store,
            note_id="n1",
            path="a.md",
            title="Alpha",
            n_chunks=1,
            links=[Link(target="x", display="X")],
        )
        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        edge = results[0]["edges"][0]
        assert set(edge.keys()) == {"target_id", "target_title", "kind", "edge_type"}

    def test_edges_dict_keys_for_edge_kind(self):
        """Typed edge (kind='edge') dicts have 5 keys, including resolved_note_id."""
        _upsert(
            self.store,
            note_id="n1",
            path="a.md",
            title="Alpha",
            n_chunks=1,
            metadata=_meta(
                title="Alpha",
                edges={"related": ["some-target"]},
                verification_state="verified",
            ),
        )
        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        edge = results[0]["edges"][0]
        assert set(edge.keys()) == {
            "target_id",
            "target_title",
            "kind",
            "edge_type",
            "resolved_note_id",
        }

    def test_edge_resolved_note_id_when_target_exists(self):
        """resolved_note_id equals target_id when the target note is in the DB."""
        # Insert both source and target notes.
        _upsert(
            self.store,
            note_id="n1",
            path="a.md",
            title="Alpha",
            n_chunks=1,
            metadata=_meta(
                title="Alpha",
                edges={"refines": ["n2"]},
                verification_state="verified",
            ),
        )
        _upsert(self.store, note_id="n2", path="b.md", title="Beta", n_chunks=1)

        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        n1 = next(r for r in results if r["note_id"] == "n1")
        edges = [e for e in n1["edges"] if e["kind"] == "edge"]
        assert len(edges) == 1
        assert edges[0]["target_id"] == "n2"
        assert edges[0]["resolved_note_id"] == "n2"

    def test_edge_resolved_note_id_none_when_target_missing(self):
        """resolved_note_id is None when the target note does not exist."""
        _upsert(
            self.store,
            note_id="n1",
            path="a.md",
            title="Alpha",
            n_chunks=1,
            metadata=_meta(
                title="Alpha",
                edges={"related": ["ghost-note"]},
                verification_state="verified",
            ),
        )

        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        assert len(results) == 1
        edges = [e for e in results[0]["edges"] if e["kind"] == "edge"]
        assert len(edges) == 1
        assert edges[0]["target_id"] == "ghost-note"
        assert edges[0]["resolved_note_id"] is None

    def test_edges_independent_per_note(self):
        """Each result only includes edges belonging to that note."""
        _upsert(
            self.store,
            note_id="n1",
            path="a.md",
            title="Alpha",
            n_chunks=1,
            links=[Link(target="link-a", display=None)],
        )
        _upsert(
            self.store,
            note_id="n2",
            path="b.md",
            title="Beta",
            n_chunks=1,
            links=[Link(target="link-b", display=None)],
        )
        results = self.store.search_notes_with_context(query_embedding=_QUERY)
        by_id = {r["note_id"]: r for r in results}
        assert by_id["n1"]["edges"][0]["target_id"] == "link-a"
        assert by_id["n2"]["edges"][0]["target_id"] == "link-b"
