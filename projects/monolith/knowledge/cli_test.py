"""Unit tests for the in-pod knowledge gardener CLI (ADR 006 Phase 4a).

Store, embedder, indexing, engine, and Session are all mocked: no real DB,
network, or embedder is touched. The disk dual-write runs for real against a
``tmp_path`` vault so collision resolution and frontmatter round-trips are
exercised end to end.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from knowledge import frontmatter
from knowledge.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Set DATABASE_URL + VAULT_ROOT and return the vault root path."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
    (tmp_path / "_processed").mkdir()
    return tmp_path


@pytest.fixture
def mocks():
    """Patch the engine/session/store/embedder/indexing seams."""
    with (
        patch("knowledge.cli.create_engine") as create_engine,
        patch("knowledge.cli.Session") as session_cls,
        patch("knowledge.cli.KnowledgeStore") as store_cls,
        patch("knowledge.cli.EmbeddingClient") as embed_cls,
        patch("knowledge.cli.index_note_from_raw", new_callable=AsyncMock) as index,
    ):
        session = MagicMock()
        session_cls.return_value.__enter__.return_value = session
        session_cls.return_value.__exit__.return_value = False

        store = MagicMock()
        store_cls.return_value = store

        embed = MagicMock()
        embed.embed = AsyncMock(return_value=[0.1] * 8)
        embed_cls.return_value = embed

        yield SimpleNamespace(
            create_engine=create_engine,
            session=session,
            store=store,
            embed=embed,
            index=index,
        )


class TestSearch:
    def test_returns_json_array_and_filters_non_finite(self, runner, env, mocks):
        mocks.store.search_notes.return_value = [
            {"note_id": "n1", "title": "T1", "path": "p1", "score": 0.91},
            {"note_id": "n2", "title": "T2", "path": "p2", "score": float("nan")},
        ]
        result = runner.invoke(app, ["search", "hello world", "--limit", "3"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert [d["note_id"] for d in data] == ["n1"]
        _, kwargs = mocks.store.search_notes.call_args
        assert kwargs["limit"] == 3


class TestGet:
    def test_found(self, runner, env, mocks):
        mocks.store.get_note_by_id.return_value = {
            "note_id": "foo",
            "title": "Foo",
            "path": "_processed/foo.md",
            "type": "atom",
            "tags": ["a"],
            "content": "Body of foo.",
        }
        mocks.store.get_note_links.return_value = [
            {"target_id": "bar", "kind": "edge", "edge_type": "related"}
        ]
        result = runner.invoke(app, ["get", "foo"])
        assert result.exit_code == 0
        out = json.loads(result.output)
        assert out["note_id"] == "foo"
        assert out["content"] == "Body of foo."
        assert out["edges"][0]["edge_type"] == "related"

    def test_not_found_exits_1(self, runner, env, mocks):
        mocks.store.get_note_by_id.return_value = None
        result = runner.invoke(app, ["get", "missing"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestGetRaw:
    def test_found_prints_content(self, runner, env, mocks):
        mocks.session.exec.return_value.first.return_value = SimpleNamespace(
            content="# raw markdown\nbody"
        )
        result = runner.invoke(app, ["get-raw", "raw-123"])
        assert result.exit_code == 0
        assert "# raw markdown" in result.output

    def test_missing_exits_1(self, runner, env, mocks):
        mocks.session.exec.return_value.first.return_value = None
        result = runner.invoke(app, ["get-raw", "raw-nope"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestCreateAtom:
    def test_happy_path(self, runner, env, mocks):
        result = runner.invoke(
            app,
            [
                "create-atom",
                "--title",
                "My Atom",
                "--body",
                "Hello body content",
                "--type",
                "atom",
                "--visibility",
                "public",
                "--tags",
                "alpha",
            ],
        )
        assert result.exit_code == 0
        assert result.output.strip() == "my-atom"

        note_file = env / "_processed" / "my-atom.md"
        assert note_file.exists()
        assert "Hello body content" in note_file.read_text()

        mocks.index.assert_called_once()
        _, kwargs = mocks.index.call_args
        assert kwargs["note_id"] == "my-atom"
        assert kwargs["rel_path"] == "_processed/my-atom.md"
        assert "Hello body content" in kwargs["raw"]
        assert "visibility: public" in kwargs["raw"]

    def test_body_from_stdin(self, runner, env, mocks):
        result = runner.invoke(
            app,
            [
                "create-atom",
                "--title",
                "Stdin Atom",
                "--body",
                "-",
                "--type",
                "fact",
                "--visibility",
                "private",
            ],
            input="piped body text",
        )
        assert result.exit_code == 0
        _, kwargs = mocks.index.call_args
        assert "piped body text" in kwargs["raw"]

    def test_missing_visibility_exits_2(self, runner, env, mocks):
        result = runner.invoke(
            app,
            [
                "create-atom",
                "--title",
                "No Vis",
                "--body",
                "x",
                "--type",
                "atom",
            ],
        )
        assert result.exit_code == 2
        mocks.index.assert_not_called()

    def test_active_without_status_size_exits_2(self, runner, env, mocks):
        result = runner.invoke(
            app,
            [
                "create-atom",
                "--title",
                "A Task",
                "--body",
                "do the thing",
                "--type",
                "active",
                "--visibility",
                "public",
            ],
        )
        assert result.exit_code == 2
        assert "requires --status and --size" in result.output
        mocks.index.assert_not_called()

    def test_active_with_status_size_ok(self, runner, env, mocks):
        result = runner.invoke(
            app,
            [
                "create-atom",
                "--title",
                "Real Task",
                "--body",
                "do it",
                "--type",
                "active",
                "--visibility",
                "public",
                "--status",
                "active",
                "--size",
                "small",
            ],
        )
        assert result.exit_code == 0
        _, kwargs = mocks.index.call_args
        assert "status: active" in kwargs["raw"]
        assert "size: small" in kwargs["raw"]

    def test_bad_edge_format_exits_2(self, runner, env, mocks):
        result = runner.invoke(
            app,
            [
                "create-atom",
                "--title",
                "Edgy",
                "--body",
                "x",
                "--type",
                "atom",
                "--visibility",
                "public",
                "--edge",
                "bogustype:target",
            ],
        )
        assert result.exit_code == 2
        assert "invalid edge type" in result.output
        mocks.index.assert_not_called()

    def test_valid_edge_serialized(self, runner, env, mocks):
        result = runner.invoke(
            app,
            [
                "create-atom",
                "--title",
                "Linked",
                "--body",
                "x",
                "--type",
                "atom",
                "--visibility",
                "public",
                "--edge",
                "derives_from:source-note",
            ],
        )
        assert result.exit_code == 0
        _, kwargs = mocks.index.call_args
        parsed, _ = frontmatter.parse(kwargs["raw"])
        assert parsed.edges["derives_from"] == ["source-note"]

    def test_collision_appends_suffix(self, runner, env, mocks):
        (env / "_processed" / "my-atom.md").write_text("---\nid: my-atom\n---\n\nx\n")
        result = runner.invoke(
            app,
            [
                "create-atom",
                "--title",
                "My Atom",
                "--body",
                "second one",
                "--type",
                "atom",
                "--visibility",
                "public",
            ],
        )
        assert result.exit_code == 0
        assert result.output.strip() == "my-atom-1"
        assert (env / "_processed" / "my-atom-1.md").exists()
        _, kwargs = mocks.index.call_args
        assert kwargs["note_id"] == "my-atom-1"

    def test_provenance_row_added_when_raw_found(self, runner, env, mocks):
        mocks.session.exec.return_value.first.return_value = SimpleNamespace(id=42)
        result = runner.invoke(
            app,
            [
                "create-atom",
                "--title",
                "Derived",
                "--body",
                "x",
                "--type",
                "atom",
                "--visibility",
                "public",
                "--derived-from-raw",
                "raw-7",
            ],
        )
        assert result.exit_code == 0
        assert mocks.session.add.called
        assert mocks.session.commit.called


class TestEdit:
    def _seed(self, env) -> None:
        (env / "_processed" / "foo.md").write_text(
            "---\nid: foo\ntitle: Old Title\ntype: atom\n"
            "aliases:\n- legacy\n---\n\nOriginal body.\n"
        )

    def test_merges_title_and_reindexes(self, runner, env, mocks):
        self._seed(env)
        mocks.store.get_note_by_id.return_value = {
            "note_id": "foo",
            "path": "_processed/foo.md",
            "title": "Old Title",
            "type": "atom",
            "tags": [],
            "content": "Original body.",
        }
        result = runner.invoke(app, ["edit", "foo", "--title", "New Title"])
        assert result.exit_code == 0
        assert result.output.strip() == "foo"

        on_disk = (env / "_processed" / "foo.md").read_text()
        assert "New Title" in on_disk
        # Pre-existing aliases must be preserved through the merge.
        assert "legacy" in on_disk

        mocks.index.assert_called_once()
        _, kwargs = mocks.index.call_args
        assert kwargs["note_id"] == "foo"
        assert "New Title" in kwargs["raw"]

    def test_not_found_exits_1(self, runner, env, mocks):
        mocks.store.get_note_by_id.return_value = None
        result = runner.invoke(app, ["edit", "ghost", "--title", "X"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestPatchEdges:
    def test_unions_edges(self, runner, env, mocks):
        (env / "_processed" / "bar.md").write_text(
            "---\nid: bar\ntitle: Bar\ntype: atom\n"
            "edges:\n  related:\n  - existing-a\n---\n\nbody\n"
        )
        mocks.store.get_note_by_id.return_value = {
            "note_id": "bar",
            "path": "_processed/bar.md",
            "title": "Bar",
            "type": "atom",
            "tags": [],
            "content": "body",
        }
        result = runner.invoke(app, ["patch-edges", "bar", "--edge", "related:new-b"])
        assert result.exit_code == 0
        assert result.output.strip() == "bar"

        _, kwargs = mocks.index.call_args
        parsed, _ = frontmatter.parse(kwargs["raw"])
        assert parsed.edges["related"] == ["existing-a", "new-b"]
