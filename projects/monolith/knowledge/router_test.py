"""Unit tests for knowledge/router.py — /search and /notes endpoints."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
import dataclasses
import knowledge.module
from framework import PRIVATE_PROFILE, build_app
from knowledge.frontmatter import ParsedFrontmatter
from knowledge.gaps import (
    GapAnswerInvalidError,
    GapNotFoundError,
    GapWrongStateError,
)
from knowledge.links import Link
from knowledge.public_models import PublicNote, PublicNoteLink
from knowledge.router import get_embedding_client
from knowledge.store import KnowledgeStore

# Compose only the knowledge domain instead of the whole monolith: the
# same framework wiring the production app gets, without depending on
# the app composition root, which imports every other domain.
app = build_app(
    dataclasses.replace(PRIVATE_PROFILE, otel_enabled=False),
    (knowledge.module.MODULE,),
)

FAKE_EMBEDDING = [0.1] * 1024

CANNED_RESULTS = [
    {
        "note_id": "n1",
        "title": "Attention Is All You Need",
        "path": "papers/attention.md",
        "type": "paper",
        "tags": ["ml", "transformers"],
        "score": 0.95,
        "snippet": "The transformer replaces recurrence entirely with attention.",
        "section": "## Architecture",
    },
]


@pytest.fixture()
def fake_embed_client():
    client = AsyncMock()
    client.embed.return_value = FAKE_EMBEDDING
    return client


@pytest.fixture()
def fake_session():
    return MagicMock()


@pytest.fixture()
def client(fake_session, fake_embed_client):
    """TestClient with overridden session and embedding client."""
    app.dependency_overrides[get_session] = lambda: fake_session
    app.dependency_overrides[get_embedding_client] = lambda: fake_embed_client
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestSearchEndpoint:
    """Tests for GET /api/knowledge/search."""

    def test_happy_path_returns_canned_results(self, client, fake_embed_client):
        """Query >= 2 chars returns results with all expected fields."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.search_notes_with_context.return_value = (
                CANNED_RESULTS
            )
            r = client.get("/api/knowledge/search?q=attention")

        assert r.status_code == 200
        body = r.json()
        assert len(body["results"]) == 1
        result = body["results"][0]
        assert result["note_id"] == "n1"
        assert result["title"] == "Attention Is All You Need"
        assert result["path"] == "papers/attention.md"
        assert result["type"] == "paper"
        assert result["tags"] == ["ml", "transformers"]
        assert result["score"] == 0.95
        assert "transformer replaces recurrence" in result["snippet"]
        assert result["section"] == "## Architecture"

        fake_embed_client.embed.assert_awaited_once_with("attention")

    def test_empty_query_returns_empty_results(self, client, fake_embed_client):
        """Empty query returns [] without calling embed or store."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            r = client.get("/api/knowledge/search?q=")

            assert r.status_code == 200
            assert r.json() == {"results": []}
            fake_embed_client.embed.assert_not_awaited()
            MockStore.assert_not_called()

    def test_single_char_query_returns_empty_results(self, client, fake_embed_client):
        """Single-char query returns [] without calling embed."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            r = client.get("/api/knowledge/search?q=a")

            assert r.status_code == 200
            assert r.json() == {"results": []}
            fake_embed_client.embed.assert_not_awaited()
            MockStore.assert_not_called()

    def test_type_filter_forwarded_to_store(self, client):
        """type query param is passed as type_filter to the store."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.search_notes_with_context.return_value = []
            client.get("/api/knowledge/search?q=attention&type=paper")

            MockStore.return_value.search_notes_with_context.assert_called_once_with(
                query_embedding=FAKE_EMBEDDING,
                limit=20,
                type_filter="paper",
            )

    def test_limit_forwarded_to_store(self, client):
        """limit query param is passed through to the store."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.search_notes_with_context.return_value = []
            client.get("/api/knowledge/search?q=attention&limit=5")

            MockStore.return_value.search_notes_with_context.assert_called_once_with(
                query_embedding=FAKE_EMBEDDING,
                limit=5,
                type_filter=None,
            )

    def test_embedding_failure_returns_503(self, fake_session):
        """Embedding client exception produces HTTP 503."""
        failing_client = AsyncMock()
        failing_client.embed.side_effect = RuntimeError("boom")
        app.dependency_overrides[get_session] = lambda: fake_session
        app.dependency_overrides[get_embedding_client] = lambda: failing_client
        try:
            c = TestClient(app, raise_server_exceptions=False)
            r = c.get("/api/knowledge/search?q=hello")
            assert r.status_code == 503
            body = r.json()
            assert body.get("detail") == "embedding unavailable"
        finally:
            app.dependency_overrides.clear()

    def test_default_limit_is_20(self, client):
        """When limit is not specified, store is called with limit=20."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.search_notes_with_context.return_value = []
            client.get("/api/knowledge/search?q=attention")

            MockStore.return_value.search_notes_with_context.assert_called_once_with(
                query_embedding=FAKE_EMBEDDING,
                limit=20,
                type_filter=None,
            )

    def test_search_results_include_edges(self, client, fake_embed_client):
        """Search results include edges with resolved_note_id for typed edges."""
        results_with_edges = [
            {
                **CANNED_RESULTS[0],
                "edges": [
                    {
                        "target_id": "n2",
                        "kind": "edge",
                        "edge_type": "refines",
                        "target_title": None,
                        "resolved_note_id": "n2",
                    },
                ],
            },
        ]
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.search_notes_with_context.return_value = (
                results_with_edges
            )
            r = client.get("/api/knowledge/search?q=attention")

        assert r.status_code == 200
        body = r.json()
        result = body["results"][0]
        assert "edges" in result
        assert result["edges"][0]["target_id"] == "n2"
        assert result["edges"][0]["edge_type"] == "refines"
        assert result["edges"][0]["resolved_note_id"] == "n2"


SAMPLE_NOTE = {
    "note_id": "n1",
    "title": "Attention Is All You Need",
    "path": "papers/attention.md",
    "type": "paper",
    "tags": ["ml", "transformers"],
}


@pytest.fixture()
def note_client(fake_session):
    """TestClient with only session override — /notes doesn't need embed."""
    app.dependency_overrides[get_session] = lambda: fake_session
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestGetNoteEndpoint:
    """Tests for GET /api/knowledge/notes/{note_id}.

    DB-only now (ADR 006, Obsidian decommissioned): the body comes from the
    note's Postgres ``content`` column via ``resolve_note_body`` — there is no
    vault directory, no on-disk file read, and no path-traversal guard.
    """

    def test_happy_path_returns_note_with_content(self, note_client):
        """Existing note returns all fields plus the body from ``content``."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.get_note_by_id.return_value = {
                **SAMPLE_NOTE,
                "content": "# Attention\n\nSelf-attention mechanism.",
            }
            MockStore.return_value.get_note_links.return_value = []
            r = note_client.get("/api/knowledge/notes/n1")

        assert r.status_code == 200
        body = r.json()
        assert body["note_id"] == "n1"
        assert body["title"] == "Attention Is All You Need"
        assert body["path"] == "papers/attention.md"
        assert body["type"] == "paper"
        assert body["tags"] == ["ml", "transformers"]
        assert body["content"] == "# Attention\n\nSelf-attention mechanism."

    def test_serves_content_from_db_without_disk(self, note_client):
        """ADR 006: body is served from Postgres ``content``, no vault file."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.get_note_by_id.return_value = {
                **SAMPLE_NOTE,
                "content": "# From Postgres\n\nNo disk read needed.",
            }
            MockStore.return_value.get_note_links.return_value = []
            r = note_client.get("/api/knowledge/notes/n1")

        assert r.status_code == 200
        body = r.json()
        assert body["content"] == "# From Postgres\n\nNo disk read needed."

    def test_missing_note_returns_404(self, note_client):
        """get_note_by_id returns None -> 404 'note not found'."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.get_note_by_id.return_value = None
            r = note_client.get("/api/knowledge/notes/nonexistent")

        assert r.status_code == 404
        body = r.json()
        assert body.get("detail") == "note not found"

    def test_missing_body_returns_404(self, note_client):
        """Note exists in DB but its ``content`` is NULL -> 404 'note has no body'."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.get_note_by_id.return_value = {
                **SAMPLE_NOTE,
                "content": None,
            }
            r = note_client.get("/api/knowledge/notes/n1")

        assert r.status_code == 404
        body = r.json()
        assert body.get("detail") == "note has no body"

    def test_note_includes_edges(self, note_client):
        """Note detail response includes edges."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.get_note_by_id.return_value = {
                **SAMPLE_NOTE,
                "content": "# Attention\n\nContent.",
            }
            MockStore.return_value.get_note_links.return_value = [
                {
                    "target_id": "n2",
                    "kind": "link",
                    "edge_type": None,
                    "target_title": "Related Note",
                },
            ]
            r = note_client.get("/api/knowledge/notes/n1")

        assert r.status_code == 200
        body = r.json()
        assert "edges" in body
        assert body["edges"][0]["target_id"] == "n2"
        assert body["edges"][0]["target_title"] == "Related Note"


# ---------------------------------------------------------------------------
# Gap lifecycle endpoint tests
# ---------------------------------------------------------------------------

SAMPLE_GAP = {
    "id": 1,
    "term": "Linkerd mTLS",
    "gap_class": "internal",
    "state": "in_review",
}


class TestListGapsEndpoint:
    """Tests for GET /api/knowledge/gaps.

    Verifies that query params are correctly forwarded to
    KnowledgeStore.list_gaps() via split_csv(), that limit bounds are
    enforced by FastAPI validation, and that the response is wrapped in
    {"gaps": [...]}.
    """

    def test_happy_path_returns_gaps(self, note_client):
        """list_gaps result is returned as {"gaps": [...]}."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_gaps.return_value = [SAMPLE_GAP]
            r = note_client.get("/api/knowledge/gaps")

        assert r.status_code == 200
        assert r.json() == {"gaps": [SAMPLE_GAP]}

    def test_empty_result_returns_empty_list(self, note_client):
        """No gaps in store returns {"gaps": []}."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_gaps.return_value = []
            r = note_client.get("/api/knowledge/gaps")

        assert r.status_code == 200
        assert r.json() == {"gaps": []}

    def test_no_filters_passes_none_to_store(self, note_client):
        """Omitting state/gap_class passes states=None, classes=None to the store."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_gaps.return_value = []
            note_client.get("/api/knowledge/gaps")

            MockStore.return_value.list_gaps.assert_called_once_with(
                states=None,
                classes=None,
                limit=100,
            )

    def test_state_filter_forwarded_to_store(self, note_client):
        """Single state value is split and forwarded as a list."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_gaps.return_value = []
            note_client.get("/api/knowledge/gaps?state=in_review")

            MockStore.return_value.list_gaps.assert_called_once_with(
                states=["in_review"],
                classes=None,
                limit=100,
            )

    def test_state_csv_split_into_list(self, note_client):
        """Comma-separated state param is split into a list by split_csv()."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_gaps.return_value = []
            note_client.get("/api/knowledge/gaps?state=in_review,classified")

            MockStore.return_value.list_gaps.assert_called_once_with(
                states=["in_review", "classified"],
                classes=None,
                limit=100,
            )

    def test_gap_class_csv_split_into_list(self, note_client):
        """Comma-separated gap_class param is split into a list by split_csv()."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_gaps.return_value = []
            note_client.get("/api/knowledge/gaps?gap_class=internal,hybrid")

            MockStore.return_value.list_gaps.assert_called_once_with(
                states=None,
                classes=["internal", "hybrid"],
                limit=100,
            )

    def test_limit_forwarded_to_store(self, note_client):
        """Explicit limit is forwarded to the store."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_gaps.return_value = []
            note_client.get("/api/knowledge/gaps?limit=50")

            MockStore.return_value.list_gaps.assert_called_once_with(
                states=None,
                classes=None,
                limit=50,
            )

    def test_default_limit_is_100(self, note_client):
        """Default limit is 100 when not specified."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_gaps.return_value = []
            note_client.get("/api/knowledge/gaps")

            MockStore.return_value.list_gaps.assert_called_once_with(
                states=None,
                classes=None,
                limit=100,
            )

    def test_limit_over_max_returns_422(self, note_client):
        """limit > 500 is rejected with HTTP 422 (FastAPI ge/le validation)."""
        r = note_client.get("/api/knowledge/gaps?limit=501")
        assert r.status_code == 422

    def test_limit_zero_returns_422(self, note_client):
        """limit=0 violates ge=1 constraint and returns HTTP 422."""
        r = note_client.get("/api/knowledge/gaps?limit=0")
        assert r.status_code == 422

    def test_trailing_comma_in_state_stripped(self, note_client):
        """Trailing comma must not produce an empty-string filter segment.

        Regression: without split_csv(), state=in_review, would pass [""]
        through as a filter value which silently hides gaps.
        """
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_gaps.return_value = []
            note_client.get("/api/knowledge/gaps?state=in_review,")

            MockStore.return_value.list_gaps.assert_called_once_with(
                states=["in_review"],
                classes=None,
                limit=100,
            )

    def test_all_comma_state_passes_none(self, note_client):
        """state=, (only commas/spaces) passes None rather than empty list."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_gaps.return_value = []
            note_client.get("/api/knowledge/gaps?state=,")

            MockStore.return_value.list_gaps.assert_called_once_with(
                states=None,
                classes=None,
                limit=100,
            )

    def test_both_filters_forwarded_together(self, note_client):
        """state and gap_class filters are both forwarded simultaneously."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_gaps.return_value = []
            note_client.get("/api/knowledge/gaps?state=in_review&gap_class=internal")

            MockStore.return_value.list_gaps.assert_called_once_with(
                states=["in_review"],
                classes=["internal"],
                limit=100,
            )


class TestReviewQueueEndpoint:
    """Tests for GET /api/knowledge/gaps/review-queue.

    Delegates to list_gaps_for_review(session, mode=..., limit=...);
    response is {"gaps": [...]}.
    """

    def test_happy_path_returns_gaps(self, note_client):
        """list_gaps_for_review result is wrapped in {"gaps": [...]}."""
        with patch("knowledge.router.list_gaps_for_review") as mock_queue:
            mock_queue.return_value = [SAMPLE_GAP]
            r = note_client.get("/api/knowledge/gaps/review-queue")

        assert r.status_code == 200
        assert r.json() == {"gaps": [SAMPLE_GAP]}

    def test_empty_queue_returns_empty_list(self, note_client):
        """Empty queue returns {"gaps": []}."""
        with patch("knowledge.router.list_gaps_for_review") as mock_queue:
            mock_queue.return_value = []
            r = note_client.get("/api/knowledge/gaps/review-queue")

        assert r.status_code == 200
        assert r.json() == {"gaps": []}

    def test_session_forwarded_to_list_gaps_for_review(self, note_client, fake_session):
        """The injected DB session is forwarded to list_gaps_for_review."""
        with patch("knowledge.router.list_gaps_for_review") as mock_queue:
            mock_queue.return_value = []
            note_client.get("/api/knowledge/gaps/review-queue")

            # The gap review-queue is pure-DB now; no vault_root kwarg.
            mock_queue.assert_called_once()
            call = mock_queue.call_args
            assert call.args == (fake_session,)
            assert call.kwargs.get("mode") == "pending"
            assert call.kwargs.get("limit") == 50
            assert "vault_root" not in call.kwargs

    def test_mode_audit_forwarded(self, note_client, fake_session):
        """mode=audit is forwarded to list_gaps_for_review."""
        with patch("knowledge.router.list_gaps_for_review") as mock_queue:
            mock_queue.return_value = []
            note_client.get("/api/knowledge/gaps/review-queue?mode=audit")

            mock_queue.assert_called_once()
            call = mock_queue.call_args
            assert call.args == (fake_session,)
            assert call.kwargs.get("mode") == "audit"
            assert call.kwargs.get("limit") == 50
            assert "vault_root" not in call.kwargs

    def test_invalid_mode_rejected(self, note_client):
        """mode= must be one of pending|audit (FastAPI Literal validation)."""
        with patch("knowledge.router.list_gaps_for_review") as mock_queue:
            mock_queue.return_value = []
            r = note_client.get("/api/knowledge/gaps/review-queue?mode=bogus")

        assert r.status_code == 422

    def test_multiple_gaps_returned_in_order(self, note_client):
        """Multiple gaps are returned in the order list_gaps_for_review provides."""
        gap_a = {**SAMPLE_GAP, "id": 1, "term": "alpha"}
        gap_b = {**SAMPLE_GAP, "id": 2, "term": "beta"}
        with patch("knowledge.router.list_gaps_for_review") as mock_queue:
            mock_queue.return_value = [gap_a, gap_b]
            r = note_client.get("/api/knowledge/gaps/review-queue")

        assert r.status_code == 200
        terms = [g["term"] for g in r.json().get("gaps", [])]
        assert terms == ["alpha", "beta"]


class TestAnswerGapEndpoint:
    """Tests for POST /api/knowledge/gaps/{gap_id}/answer.

    The endpoint accepts {"answer": "..."}, delegates to answer_gap(), and
    maps ValueError sub-types to specific HTTP status codes:
      - "Gap not found"        → 404
      - "expected 'in_review'" → 409
      - "frontmatter terminator" → 400
      - any other ValueError   → 400
    """

    def test_happy_path_returns_answer_gap_result(self, note_client):
        """Successful answer_gap() result is returned directly."""
        expected = {
            "gap_id": 1,
            "note_id": "linkerd-mtls",
        }
        with patch("knowledge.router.answer_gap") as mock_answer:
            mock_answer.return_value = expected
            r = note_client.post(
                "/api/knowledge/gaps/1/answer",
                json={"answer": "Linkerd uses per-pod sidecars on port 4143."},
            )

        assert r.status_code == 200
        assert r.json() == expected

    def test_answer_and_gap_id_forwarded_to_answer_gap(self, note_client):
        """gap_id and answer string are forwarded positionally to answer_gap."""
        with patch("knowledge.router.answer_gap") as mock_answer:
            mock_answer.return_value = {"gap_id": 42, "note_id": "x"}
            note_client.post(
                "/api/knowledge/gaps/42/answer",
                json={"answer": "my answer text"},
            )

            args, _ = mock_answer.call_args
            # answer_gap(session, gap_id, answer)
            assert args[1] == 42
            assert args[2] == "my answer text"

    def test_gap_not_found_returns_404(self, note_client):
        """GapNotFoundError maps to HTTP 404."""
        with patch("knowledge.router.answer_gap") as mock_answer:
            mock_answer.side_effect = GapNotFoundError("Gap not found: id=9999")
            r = note_client.post(
                "/api/knowledge/gaps/9999/answer",
                json={"answer": "anything"},
            )

        assert r.status_code == 404
        assert "Gap not found" in r.json().get("detail", "")

    def test_wrong_state_returns_409(self, note_client):
        """GapWrongStateError maps to HTTP 409."""
        with patch("knowledge.router.answer_gap") as mock_answer:
            mock_answer.side_effect = GapWrongStateError(
                "Gap 1 is in state 'discovered', expected 'in_review'"
            )
            r = note_client.post(
                "/api/knowledge/gaps/1/answer",
                json={"answer": "x"},
            )

        assert r.status_code == 409
        assert "expected 'in_review'" in r.json().get("detail", "")

    def test_frontmatter_terminator_returns_400(self, note_client):
        """GapAnswerInvalidError (frontmatter terminator) maps to HTTP 400."""
        with patch("knowledge.router.answer_gap") as mock_answer:
            mock_answer.side_effect = GapAnswerInvalidError(
                "Answer contains a frontmatter terminator (---)"
            )
            r = note_client.post(
                "/api/knowledge/gaps/1/answer",
                json={"answer": "foo\n---\nbar"},
            )

        assert r.status_code == 400
        assert "frontmatter terminator" in r.json().get("detail", "")

    def test_other_value_error_returns_400(self, note_client):
        """Any other ValueError (not matched by the three specific checks) → 400."""
        with patch("knowledge.router.answer_gap") as mock_answer:
            mock_answer.side_effect = ValueError("some unexpected validation failure")
            r = note_client.post(
                "/api/knowledge/gaps/1/answer",
                json={"answer": "x"},
            )

        assert r.status_code == 400

    def test_missing_answer_field_returns_422(self, note_client):
        """Request body without 'answer' field is rejected by FastAPI with 422."""
        r = note_client.post(
            "/api/knowledge/gaps/1/answer",
            json={},
        )
        assert r.status_code == 422

    def test_error_detail_message_preserved(self, note_client):
        """The ValueError message is preserved verbatim in the detail field."""
        msg = "Gap not found: id=777"
        with patch("knowledge.router.answer_gap") as mock_answer:
            mock_answer.side_effect = ValueError(msg)
            r = note_client.post(
                "/api/knowledge/gaps/777/answer",
                json={"answer": "x"},
            )

        assert r.json().get("detail") == msg


# The _meta/_upsert helpers below mirror the equivalents in store_test.py
# (we don't import them across test files because Bazel's per-test py_test
# targets exclude sibling *_test.py srcs).
def _meta(**kw):
    return ParsedFrontmatter(**kw)


def _chunks(n):
    return [
        {"index": i, "section_header": f"H{i}", "text": f"chunk {i}"} for i in range(n)
    ]


def _vecs(n):
    return [[float(i)] * 1024 for i in range(n)]


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
):
    metadata = metadata or _meta(title=title)
    store.upsert_note(
        note_id=note_id,
        path=path,
        content_hash=content_hash,
        title=title,
        metadata=metadata,
        chunks=_chunks(n_chunks),
        vectors=_vecs(n_chunks),
        links=links or [],
    )


@pytest.fixture()
def real_session():
    """Real in-memory SQLite session — mirrors store_test.py's session fixture."""
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
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


class TestGraphEndpoint:
    """Tests for GET /api/knowledge/graph."""

    def test_graph_endpoint_returns_nodes_edges_and_cache_header(self, real_session):
        store = KnowledgeStore(real_session)
        # Seed two atom notes with a body wikilink from id-a -> id-b.
        # n_chunks=0: get_graph() doesn't read Chunk/embeddings; keep test scope tight.
        _upsert(
            store,
            note_id="id-a",
            path="a.md",
            content_hash="h-a",
            title="A",
            metadata=_meta(title="A", type="atom"),
            n_chunks=0,
            links=[Link(target="id-b", display=None)],
        )
        _upsert(
            store,
            note_id="id-b",
            path="b.md",
            content_hash="h-b",
            title="B",
            metadata=_meta(title="B", type="atom"),
            n_chunks=0,
        )

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            response = c.get("/api/knowledge/graph")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert {n["id"] for n in body["nodes"]} == {"id-a", "id-b"}
        assert len(body["edges"]) == 1
        assert body["edges"][0]["source"] == "id-a"
        assert body["edges"][0]["target"] == "id-b"

        cache_control = response.headers["cache-control"]
        assert "public" in cache_control
        assert "s-maxage=3600" in cache_control
        assert "stale-while-revalidate=" in cache_control
        assert "stale-if-error=" in cache_control

        # Conditional GET prerequisites: a stable ETag and a Last-Modified.
        assert response.headers["etag"]
        assert response.headers["last-modified"]

    def test_graph_endpoint_returns_304_on_matching_if_none_match(self, real_session):
        store = KnowledgeStore(real_session)
        _upsert(
            store,
            note_id="id-a",
            path="a.md",
            content_hash="h-a",
            title="A",
            metadata=_meta(title="A", type="atom"),
            n_chunks=0,
        )

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            first = c.get("/api/knowledge/graph")
            etag = first.headers["etag"]
            second = c.get("/api/knowledge/graph", headers={"If-None-Match": etag})
        finally:
            app.dependency_overrides.clear()

        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["etag"] == etag
        assert "s-maxage=3600" in second.headers["cache-control"]


def _seed_public_note(
    session,
    *,
    note_id,
    title="T",
    type="atom",
    content=None,
    indexed_at=None,
    layout_x=None,
    layout_y=None,
    tags=None,
    aliases=None,
    path=None,
):
    """Insert a public_api.knowledge_notes view row (a plain SQLite table here)."""
    note = PublicNote(
        note_id=note_id,
        title=title,
        type=type,
        content=content,
        indexed_at=indexed_at or datetime.now(timezone.utc),
        layout_x=layout_x,
        layout_y=layout_y,
        tags=tags or [],
        aliases=aliases or [],
        path=path or f"{note_id}.md",
    )
    session.add(note)
    session.commit()
    return note


def _seed_public_link(session, *, source, target, kind="link", edge_type=None):
    """Insert a public_api.knowledge_note_links view row."""
    link = PublicNoteLink(source=source, target=target, kind=kind, edge_type=edge_type)
    session.add(link)
    session.commit()
    return link


class TestPublicGraphEndpoint:
    """Tests for GET /api/knowledge/public/graph.

    Reads the ``public_api`` views (PublicNote / PublicNoteLink), which already
    restrict to public, non-deleted rows at the DB layer. These handler tests
    seed those view rows directly (the real_session fixture's schema-strip makes
    them plain SQLite tables); the view's visibility/deleted derivation is
    covered separately by the real-Postgres confidentiality test. What remains
    exercised here is the handler logic: the type filter, the app-side target
    resolution against the public node set, degree counting, and the cache/ETag
    contract.
    """

    def test_public_graph_resolves_targets_against_public_set(self, real_session):
        """Edges whose target is not in the public node set are dropped by the
        handler's slug resolution, even though the source is public."""
        _seed_public_note(real_session, note_id="pub-A", title="Pub A", type="atom")
        _seed_public_note(real_session, note_id="pub-B", title="Pub B", type="atom")
        # pub-A -> pub-B is kept; pub-A -> priv-X is dropped because priv-X is
        # not in the (seeded) public node set.
        _seed_public_link(real_session, source="pub-A", target="pub-B")
        _seed_public_link(real_session, source="pub-A", target="priv-X")

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            res = c.get("/api/knowledge/public/graph")
        finally:
            app.dependency_overrides.clear()

        assert res.status_code == 200
        body = res.json()
        node_ids = {n["id"] for n in body["nodes"]}
        assert node_ids == {"pub-A", "pub-B"}
        edge_pairs = {(e["source"], e["target"]) for e in body["edges"]}
        assert edge_pairs == {("pub-A", "pub-B")}

    def test_public_graph_excludes_non_renderable_types(self, real_session):
        """Notes whose type is outside GRAPH_NOTE_TYPES never appear as nodes."""
        _seed_public_note(real_session, note_id="pub-A", title="Pub A", type="atom")
        # type='journal' is not in GRAPH_NOTE_TYPES, so the handler's type
        # filter drops it.
        _seed_public_note(
            real_session, note_id="journal-1", title="Journal", type="journal"
        )

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            res = c.get("/api/knowledge/public/graph")
        finally:
            app.dependency_overrides.clear()

        body = res.json()
        node_ids = {n["id"] for n in body["nodes"]}
        assert node_ids == {"pub-A"}

    def test_public_graph_cache_headers(self, real_session):
        """Public graph carries the same CDN cache directives as /graph."""
        _seed_public_note(real_session, note_id="pub-A", title="Pub A", type="atom")

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            res = c.get("/api/knowledge/public/graph")
        finally:
            app.dependency_overrides.clear()

        cc = res.headers.get("cache-control", "")
        assert "s-maxage=3600" in cc
        assert "stale-while-revalidate=86400" in cc
        # Conditional GET prerequisites: a stable ETag and a Last-Modified.
        assert res.headers["etag"]
        assert res.headers["last-modified"]

    def test_public_graph_304_on_matching_if_none_match(self, real_session):
        """A matching If-None-Match yields an empty 304 with the same ETag."""
        _seed_public_note(real_session, note_id="pub-A", title="Pub A", type="atom")

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            first = c.get("/api/knowledge/public/graph")
            etag = first.headers["etag"]
            second = c.get(
                "/api/knowledge/public/graph", headers={"If-None-Match": etag}
            )
        finally:
            app.dependency_overrides.clear()

        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["etag"] == etag

    def test_public_graph_etag_changes_when_public_set_mutates(self, real_session):
        """Adding a new public note invalidates the ETag (node-count
        component changes even if timestamps don't move enough)."""
        _seed_public_note(real_session, note_id="pub-A", title="Pub A", type="atom")

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            etag_a = c.get("/api/knowledge/public/graph").headers["etag"]
            _seed_public_note(real_session, note_id="pub-B", title="Pub B", type="atom")
            etag_b = c.get("/api/knowledge/public/graph").headers["etag"]
        finally:
            app.dependency_overrides.clear()

        assert etag_a != etag_b

    def test_public_graph_returns_indexed_at_in_body(self, real_session):
        """StatusBar in the SvelteKit page reads data.graph.indexed_at."""
        _seed_public_note(real_session, note_id="pub-A", title="Pub A", type="atom")

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            res = c.get("/api/knowledge/public/graph")
        finally:
            app.dependency_overrides.clear()

        body = res.json()
        assert "indexed_at" in body
        parsed = datetime.fromisoformat(body["indexed_at"])
        assert parsed.tzinfo is not None  # always UTC-aware

    def test_public_graph_indexed_at_null_when_no_public_notes(self, real_session):
        """Empty public set → indexed_at is JSON null (not missing), so
        the frontend can treat it as a known absence."""
        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            res = c.get("/api/knowledge/public/graph")
        finally:
            app.dependency_overrides.clear()

        body = res.json()
        assert body["nodes"] == []
        assert body["indexed_at"] is None

    def test_public_graph_returns_layout_coords(self, real_session):
        """The handler returns the view's (already-coalesced) layout columns
        verbatim as each node's x/y. The public-vs-full COALESCE itself now
        lives in the view, so it is not re-tested here."""
        _seed_public_note(
            real_session,
            note_id="pub-A",
            title="Pub A",
            type="atom",
            layout_x=0.7,
            layout_y=0.8,
        )

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            res = c.get("/api/knowledge/public/graph")
        finally:
            app.dependency_overrides.clear()

        body = res.json()
        nodes = body["nodes"]
        assert len(nodes) == 1
        assert nodes[0]["x"] == 0.7
        assert nodes[0]["y"] == 0.8


class TestPublicNoteEndpoint:
    """Tests for GET /api/knowledge/public/notes/{note_id}.

    Strict-visibility per-note endpoint paired with /public/graph. The
    ``public_api`` view returns a row only when the note is public +
    non-deleted, so the handler treats a private/deleted note exactly like a
    missing one (covered against a real Postgres in
    public_knowledge_views_test.py). These SQLite tests seed PublicNote rows
    directly and exercise the handler logic:

    1. Identical 404 for every note the view does not expose.
    2. Body wikilinks to non-public targets are stripped to plain text via
       :func:`strip_private_wikilinks`; wikilinks to public notes are kept.
    """

    def test_public_note_200_for_public(self, real_session):
        # Body of record is Postgres content (ADR 006), so the view's
        # ``content`` column is served directly without touching the vault.
        _seed_public_note(
            real_session,
            note_id="pub-A",
            title="Pub A",
            type="atom",
            content="Hello [[pub-B]] world.",
        )
        _seed_public_note(
            real_session, note_id="pub-B", title="Pub B", type="atom", content="B body."
        )

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            res = c.get("/api/knowledge/public/notes/pub-A")
        finally:
            app.dependency_overrides.clear()

        assert res.status_code == 200
        body = res.json()
        # Public-target wikilink left intact for the frontend to resolve.
        assert "[[pub-B]]" in body["body"]

    def test_public_note_404_for_missing(self, real_session):
        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            res = c.get("/api/knowledge/public/notes/does-not-exist")
        finally:
            app.dependency_overrides.clear()

        assert res.status_code == 404

    def test_public_note_404_identical_for_all_misses(self, real_session):
        """Every note the view does not expose (private, deleted, or simply
        missing) returns a byte-identical 404, so existence is never leaked.

        In SQLite the view is materialized as a plain table seeded only with
        public rows, so an unexposed note is indistinguishable from a missing
        one here; the real-Postgres test asserts a genuinely private note 404s
        identically."""
        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            res_a = c.get("/api/knowledge/public/notes/priv-X")
            res_b = c.get("/api/knowledge/public/notes/does-not-exist")
        finally:
            app.dependency_overrides.clear()

        assert res_a.status_code == 404
        assert res_b.status_code == 404
        assert res_a.json() == res_b.json()

    def test_public_note_strips_private_wikilinks_from_body(self, real_session):
        _seed_public_note(
            real_session,
            note_id="pub-A",
            title="Pub A",
            type="atom",
            content="See [[pub-B]] and avoid [[Some Colleague]].",
        )
        _seed_public_note(
            real_session, note_id="pub-B", title="Pub B", type="atom", content="B."
        )
        # "Some Colleague" is NOT a seeded public note, so the link is stripped.

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            res = c.get("/api/knowledge/public/notes/pub-A")
        finally:
            app.dependency_overrides.clear()

        assert res.status_code == 200
        payload = res.json()
        body = payload["body"]
        assert "[[pub-B]]" in body
        assert "[[Some Colleague]]" not in body
        # Bracket text is preserved — strip_private_wikilinks drops the
        # [[…]] wrapper but keeps the display text.
        assert "Some Colleague" in body

    def test_public_note_response_excludes_internal_fields(self, real_session):
        """The response is an explicit whitelist, so no internal columns leak.

        The view itself omits ``extra`` (and every other private column), and
        the handler serializes a fixed field set rather than ``**note.dict()``.
        """
        _seed_public_note(
            real_session,
            note_id="pub-A",
            title="Pub A",
            type="atom",
            content="Hello world.",
        )

        app.dependency_overrides[get_session] = lambda: real_session
        try:
            c = TestClient(app, raise_server_exceptions=False)
            res = c.get("/api/knowledge/public/notes/pub-A")
        finally:
            app.dependency_overrides.clear()

        assert res.status_code == 200
        body = res.json()
        assert set(body.keys()) == {
            "note_id",
            "title",
            "tags",
            "aliases",
            "indexed_at",
            "body",
        }
        assert "extra" not in body
