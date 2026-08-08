"""Tests for knowledge CLI subcommands.

Uses FastAPI TestClient to exercise the full round-trip:
CLI command -> httpx call -> FastAPI handler -> response -> formatted output.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from knowledge.gardener import MAX_GARDENER_RETRIES
from knowledge.models import AtomRawProvenance, RawInput
from knowledge.router import get_embedding_client
from tools.cli.main import app


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def session():
    from sqlmodel import Session, SQLModel, create_engine
    from sqlmodel.pool import StaticPool

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
        with Session(engine) as s:
            yield s
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(autouse=True)
def _patch_fastapi(session):
    """Point the CLI's httpx calls at the FastAPI TestClient.

    FastAPI's TestClient is httpx-compatible, so we patch knowledge._client
    to return it directly. We also patch get_cf_token to avoid reading real
    cloudflared files.
    """
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    from core.db import get_session

    fastapi_app.dependency_overrides[get_session] = lambda: session
    test_client = TestClient(fastapi_app)

    @contextmanager
    def _fake_client():
        yield test_client

    with (
        patch("tools.cli.knowledge_cmd._client", _fake_client),
        patch("tools.cli.knowledge_cmd.get_cf_token", return_value="fake-token"),
    ):
        yield

    fastapi_app.dependency_overrides.clear()


def _make_raw(session, *, raw_id="raw-1", path="raw/test.md", source="test"):
    raw = RawInput(
        raw_id=raw_id,
        path=path,
        source=source,
        content="test content",
        content_hash="abc123",
    )
    session.add(raw)
    session.commit()
    session.refresh(raw)
    return raw


def _make_dead_letter(session, raw, *, error="boom", retry_count=3):
    prov = AtomRawProvenance(
        raw_fk=raw.id,
        derived_note_id="failed",
        gardener_version="test-v1",
        error=error,
        retry_count=retry_count,
    )
    session.add(prov)
    session.commit()
    session.refresh(prov)
    return prov


# ---------------------------------------------------------------------------
# Canned data shared across search / note tests
# ---------------------------------------------------------------------------

_FAKE_EMBEDDING = [0.1] * 1024

_CANNED_RESULTS = [
    {
        "note_id": "n1",
        "title": "Attention Is All You Need",
        "path": "papers/attention.md",
        "type": "paper",
        "tags": ["ml", "transformers"],
        "score": 0.95,
        "snippet": "The transformer replaces recurrence entirely with attention.",
        "section": "## Architecture",
        "edges": [],
    },
]

_CANNED_RESULTS_WITH_EDGES = [
    {
        **_CANNED_RESULTS[0],
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

_SAMPLE_NOTE = {
    "note_id": "n1",
    "title": "Attention Is All You Need",
    "path": "papers/attention.md",
    "type": "paper",
    "tags": ["ml", "transformers"],
    # ADR 006: the body of record is the Postgres ``content`` column; the
    # endpoint serves it directly (no vault file fallback anymore).
    "content": "# Attention\n\nSelf-attention mechanism.",
}


def _fake_embed_override():
    """Return a FastAPI DI factory that yields a fake async EmbeddingClient."""
    fake = AsyncMock()
    fake.embed.return_value = _FAKE_EMBEDDING
    return lambda: fake


class TestDeadLetters:
    def test_lists_dead_letters(self, runner, session):
        raw = _make_raw(session)
        _make_dead_letter(session, raw, retry_count=MAX_GARDENER_RETRIES)
        result = runner.invoke(app, ["knowledge", "dead-letters"])
        assert result.exit_code == 0
        assert "raw/test.md" in result.output
        assert "boom" in result.output

    def test_empty_list(self, runner, session):
        result = runner.invoke(app, ["knowledge", "dead-letters"])
        assert result.exit_code == 0
        assert "No dead letters" in result.output

    def test_json_output(self, runner, session):
        raw = _make_raw(session)
        _make_dead_letter(session, raw, retry_count=MAX_GARDENER_RETRIES)
        result = runner.invoke(app, ["knowledge", "dead-letters", "--json"])
        assert result.exit_code == 0
        assert '"items"' in result.output


class TestReplay:
    def test_replays_dead_letter(self, runner, session):
        raw = _make_raw(session)
        _make_dead_letter(session, raw, retry_count=MAX_GARDENER_RETRIES)
        result = runner.invoke(app, ["knowledge", "replay", str(raw.id)])
        assert result.exit_code == 0
        assert "Replayed" in result.output

    def test_404_for_unknown(self, runner, session):
        result = runner.invoke(app, ["knowledge", "replay", "9999"])
        assert result.exit_code == 1


class TestSearch:
    """Tests for the `knowledge search` CLI command."""

    def test_empty_query_returns_no_results(self, runner):
        """Single-char query hits the router's 2-char fast-path → 'No results.'"""
        result = runner.invoke(app, ["knowledge", "search", "x"])
        assert result.exit_code == 0
        assert "No results." in result.output

    def test_search_with_results(self, runner):
        """Successful search prints score, note_id, title, and type."""
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides[get_embedding_client] = _fake_embed_override()
        try:
            with patch("knowledge.router.KnowledgeStore") as MockStore:
                MockStore.return_value.search_notes_with_context.return_value = (
                    _CANNED_RESULTS
                )
                result = runner.invoke(app, ["knowledge", "search", "attention"])
        finally:
            del fastapi_app.dependency_overrides[get_embedding_client]

        assert result.exit_code == 0
        assert "0.95" in result.output
        assert "n1" in result.output
        assert "Attention Is All You Need" in result.output
        assert "paper" in result.output

    def test_search_no_results(self, runner):
        """Store returning [] prints 'No results.'"""
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides[get_embedding_client] = _fake_embed_override()
        try:
            with patch("knowledge.router.KnowledgeStore") as MockStore:
                MockStore.return_value.search_notes_with_context.return_value = []
                result = runner.invoke(app, ["knowledge", "search", "nothing here"])
        finally:
            del fastapi_app.dependency_overrides[get_embedding_client]

        assert result.exit_code == 0
        assert "No results." in result.output

    def test_json_flag(self, runner):
        """--json flag emits raw JSON instead of formatted lines."""
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides[get_embedding_client] = _fake_embed_override()
        try:
            with patch("knowledge.router.KnowledgeStore") as MockStore:
                MockStore.return_value.search_notes_with_context.return_value = (
                    _CANNED_RESULTS
                )
                result = runner.invoke(
                    app, ["knowledge", "search", "--json", "attention"]
                )
        finally:
            del fastapi_app.dependency_overrides[get_embedding_client]

        assert result.exit_code == 0
        assert '"results"' in result.output
        assert '"note_id"' in result.output
        assert '"n1"' in result.output

    def test_type_filter_forwarded(self, runner):
        """--type value is forwarded to the store as type_filter."""
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides[get_embedding_client] = _fake_embed_override()
        try:
            with patch("knowledge.router.KnowledgeStore") as MockStore:
                MockStore.return_value.search_notes_with_context.return_value = []
                runner.invoke(
                    app, ["knowledge", "search", "--type", "paper", "attention"]
                )
                MockStore.return_value.search_notes_with_context.assert_called_once_with(
                    query_embedding=_FAKE_EMBEDDING,
                    limit=10,
                    type_filter="paper",
                )
        finally:
            del fastapi_app.dependency_overrides[get_embedding_client]

    def test_limit_forwarded(self, runner):
        """--limit value is forwarded to the store."""
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides[get_embedding_client] = _fake_embed_override()
        try:
            with patch("knowledge.router.KnowledgeStore") as MockStore:
                MockStore.return_value.search_notes_with_context.return_value = []
                runner.invoke(app, ["knowledge", "search", "--limit", "5", "attention"])
                MockStore.return_value.search_notes_with_context.assert_called_once_with(
                    query_embedding=_FAKE_EMBEDDING,
                    limit=5,
                    type_filter=None,
                )
        finally:
            del fastapi_app.dependency_overrides[get_embedding_client]

    def test_search_with_edges_displays_edge_info(self, runner):
        """Results with typed edges render edge type and target in output."""
        from app.main import app as fastapi_app

        fastapi_app.dependency_overrides[get_embedding_client] = _fake_embed_override()
        try:
            with patch("knowledge.router.KnowledgeStore") as MockStore:
                MockStore.return_value.search_notes_with_context.return_value = (
                    _CANNED_RESULTS_WITH_EDGES
                )
                result = runner.invoke(app, ["knowledge", "search", "attention"])
        finally:
            del fastapi_app.dependency_overrides[get_embedding_client]

        assert result.exit_code == 0
        assert "refines" in result.output
        assert "n2" in result.output


class TestNote:
    """Tests for the `knowledge note` CLI command."""

    def test_note_fetches_metadata_and_writes_tmpfile(self, runner, tmp_path):
        """Successful note fetch prints title/type and writes content to tmpfile."""
        with (
            patch("knowledge.router.KnowledgeStore") as MockStore,
            patch("tools.cli.output.TMPDIR", tmp_path / "notes"),
        ):
            MockStore.return_value.get_note_by_id.return_value = _SAMPLE_NOTE
            MockStore.return_value.get_note_links.return_value = []
            result = runner.invoke(app, ["knowledge", "note", "n1"])

        assert result.exit_code == 0
        assert "Attention Is All You Need" in result.output
        assert "paper" in result.output
        assert "Content:" in result.output

    def test_note_json_output(self, runner):
        """--json flag emits raw JSON for the note instead of formatted text."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.get_note_by_id.return_value = _SAMPLE_NOTE
            MockStore.return_value.get_note_links.return_value = []
            result = runner.invoke(app, ["knowledge", "note", "--json", "n1"])

        assert result.exit_code == 0
        assert '"note_id"' in result.output
        assert '"n1"' in result.output
        assert '"title"' in result.output

    def test_note_displays_typed_edges(self, runner, tmp_path):
        """Note with typed edges shows 'Edges:' line with edge type and target."""
        with (
            patch("knowledge.router.KnowledgeStore") as MockStore,
            patch("tools.cli.output.TMPDIR", tmp_path / "notes"),
        ):
            MockStore.return_value.get_note_by_id.return_value = _SAMPLE_NOTE
            MockStore.return_value.get_note_links.return_value = [
                {
                    "target_id": "n2",
                    "kind": "edge",
                    "edge_type": "refines",
                    "target_title": None,
                    "resolved_note_id": "n2",
                },
            ]
            result = runner.invoke(app, ["knowledge", "note", "n1"])

        assert result.exit_code == 0
        assert "Edges:" in result.output
        assert "refines" in result.output
        assert "n2" in result.output

    def test_note_not_found_exits_nonzero(self, runner):
        """Requesting a non-existent note ID causes a non-zero exit code."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.get_note_by_id.return_value = None
            result = runner.invoke(app, ["knowledge", "note", "does-not-exist"])

        assert result.exit_code != 0
