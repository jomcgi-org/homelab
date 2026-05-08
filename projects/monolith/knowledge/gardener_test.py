"""Tests for the knowledge gardener."""

import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.gardener import (
    Gardener,
    _CLAUDE_PROMPT_HEADER,
    _DISTILL_PROMPT,
    _slugify,
    _split_frontmatter,
)
from knowledge.visibility import VISIBILITY_CRITERIA


def test_gardener_prompt_includes_visibility_criteria():
    """The atom-creation prompt must require a `visibility:` frontmatter field
    and embed the verbatim criteria text from `knowledge.visibility`.

    Drift between the criteria the LLM sees here and the criteria the public
    routes / sanitizer enforce is the primary leakage failure mode for the
    public-notes feature, so this test asserts on the imported constant.
    """
    rendered = _CLAUDE_PROMPT_HEADER.format(
        raw_id="r", raw_file_path="x", processed_root="p", title="t"
    )
    assert "visibility: public|private" in rendered
    # criteria must be appended verbatim — drift = leak
    assert VISIBILITY_CRITERIA.strip() in rendered


def _write(tmp_path: Path, rel: str, content: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


class TestDiscoverRawFiles:
    def test_finds_md_files_outside_processed_and_raw(self, tmp_path):
        _write(tmp_path, "inbox/new-note.md", "---\ntitle: New\n---\nBody.")
        _write(tmp_path, "_processed/existing.md", "---\nid: e\ntitle: E\n---\nBody.")
        _write(
            tmp_path,
            "_raw/2026/04/09/abc-old.md",
            "---\ntitle: Old\n---\nBody.",
        )
        gardener = Gardener(vault_root=tmp_path)
        raw = gardener._discover_raw_files()
        assert len(raw) == 1
        assert raw[0].name == "new-note.md"

    def test_ignores_non_md_files(self, tmp_path):
        _write(tmp_path, "inbox/image.png", "not markdown")
        _write(tmp_path, "inbox/note.md", "---\ntitle: Note\n---\nBody.")
        gardener = Gardener(vault_root=tmp_path)
        raw = gardener._discover_raw_files()
        assert len(raw) == 1

    def test_ignores_dotfiles_and_dot_directories(self, tmp_path):
        _write(tmp_path, ".obsidian/config.md", "config")
        _write(tmp_path, "inbox/.hidden.md", "hidden")
        _write(tmp_path, "inbox/visible.md", "---\ntitle: V\n---\nBody.")
        gardener = Gardener(vault_root=tmp_path)
        raw = gardener._discover_raw_files()
        assert len(raw) == 1


class TestMaxFilesPerRun:
    @pytest.mark.asyncio
    async def test_cap_limits_ingest_to_max_files(self, tmp_path, session):
        """run() processes at most max_files_per_run raw files per cycle,
        leaving the remainder for a future tick."""
        for i in range(5):
            _write(tmp_path, f"inbox/note-{i}.md", f"---\ntitle: N{i}\n---\nBody {i}.")
        gardener = Gardener(vault_root=tmp_path, max_files_per_run=2, session=session)
        calls: list[Path] = []

        async def fake_ingest(path: Path) -> None:
            calls.append(path)

        gardener._ingest_one = fake_ingest  # type: ignore[method-assign]
        stats = await gardener.run()
        assert len(calls) == 2
        assert stats.ingested == 2
        assert stats.failed == 0

    @pytest.mark.asyncio
    async def test_cap_disabled_when_zero_or_negative(self, tmp_path, session):
        """max_files_per_run <= 0 disables the cap."""
        for i in range(3):
            _write(tmp_path, f"inbox/note-{i}.md", f"---\ntitle: N{i}\n---\nBody.")
        gardener = Gardener(vault_root=tmp_path, max_files_per_run=0, session=session)
        calls: list[Path] = []

        async def fake_ingest(path: Path) -> None:
            calls.append(path)

        gardener._ingest_one = fake_ingest  # type: ignore[method-assign]
        stats = await gardener.run()
        assert len(calls) == 3
        assert stats.ingested == 3


class TestIngestOneClaude:
    @pytest.mark.asyncio
    async def test_prompt_includes_full_raw_content_for_frontmatter_only_note(
        self, tmp_path
    ):
        """The prompt references the raw file by path so Claude reads it via the Read tool.

        Previously the raw content was inlined in the prompt, but large files
        (e.g. YouTube transcripts) exceed Linux ARG_MAX. Now the prompt just
        contains the file path.
        """
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "book.md"
        raw = (
            "---\n"
            "title: The Staff Engineer's Path\n"
            "author: Tanya Reilly\n"
            "description: A book about staff engineering.\n"
            "isbn13: 9781098118709\n"
            "---\n"
        )
        note.write_text(raw)
        processed = vault / "_processed"
        processed.mkdir()

        proc_mock = AsyncMock()
        proc_mock.returncode = 0

        async def fake_communicate():
            (processed / "staff-path.md").write_text(
                "---\nid: staff-path\ntitle: Staff Path\ntype: atom\n---\nbody"
            )
            return b"", b""

        proc_mock.communicate = fake_communicate

        with patch(
            "asyncio.create_subprocess_exec", return_value=proc_mock
        ) as mock_exec:
            await Gardener(vault_root=vault)._ingest_one(note)

        args = mock_exec.call_args[0]
        # The prompt is passed via the -p flag as the last positional arg
        p_idx = list(args).index("-p")
        prompt = args[p_idx + 1]
        assert str(note) in prompt, "raw file path must appear in prompt"
        assert "Tanya Reilly" not in prompt, "raw content must not be inlined"

    @pytest.mark.asyncio
    async def test_prompt_contains_absolute_path_not_just_filename(self, tmp_path):
        """The prompt must embed the full absolute path, not just the basename.

        Claude's Read tool requires an absolute path to locate the file. If
        only the filename is embedded, Claude cannot open the file and will
        either error or decompose nothing.
        """
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "my-note.md"
        note.write_text("# My Note\nsome body content")
        processed = vault / "_processed"
        processed.mkdir()

        proc_mock = AsyncMock()
        proc_mock.returncode = 0

        async def fake_communicate():
            (processed / "my-note.md").write_text(
                "---\nid: my-note\ntitle: My Note\ntype: atom\n---\nbody"
            )
            return b"", b""

        proc_mock.communicate = fake_communicate

        with patch(
            "asyncio.create_subprocess_exec", return_value=proc_mock
        ) as mock_exec:
            await Gardener(vault_root=vault)._ingest_one(note)

        args = mock_exec.call_args[0]
        p_idx = list(args).index("-p")
        prompt = args[p_idx + 1]
        # Full absolute path must be present
        assert str(note) in prompt, "absolute file path must be in prompt"
        # Basename alone is not sufficient — path must include parent dir
        assert str(note.parent) in prompt, "parent directory must be in prompt path"

    @pytest.mark.asyncio
    async def test_prompt_does_not_contain_body_text_for_plain_note(self, tmp_path):
        """A note with only a markdown body must not have its body inlined in the prompt.

        Commit 856d785 removed the raw_text concatenation. This test verifies
        body-only notes (no frontmatter) are handled the same way — the body
        content must not appear in the prompt string sent to claude.
        """
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "plain.md"
        note.write_text("This is the unique body content that must not appear.")
        processed = vault / "_processed"
        processed.mkdir()

        proc_mock = AsyncMock()
        proc_mock.returncode = 0

        async def fake_communicate():
            (processed / "plain.md").write_text(
                "---\nid: plain\ntitle: Plain\ntype: atom\n---\nbody"
            )
            return b"", b""

        proc_mock.communicate = fake_communicate

        with patch(
            "asyncio.create_subprocess_exec", return_value=proc_mock
        ) as mock_exec:
            await Gardener(vault_root=vault)._ingest_one(note)

        args = mock_exec.call_args[0]
        p_idx = list(args).index("-p")
        prompt = args[p_idx + 1]
        assert "unique body content that must not appear" not in prompt

    @pytest.mark.asyncio
    async def test_prompt_does_not_contain_frontmatter_or_body_for_mixed_note(
        self, tmp_path
    ):
        """A note with both frontmatter and body must have neither inlined in the prompt.

        Previously both the frontmatter YAML and the body were appended. After
        856d785 neither should appear — only the file path is embedded.
        """
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "mixed.md"
        note.write_text(
            "---\n"
            "title: Mixed Note\n"
            "author: SecretAuthor\n"
            "---\n"
            "This is the distinctive body text.\n"
        )
        processed = vault / "_processed"
        processed.mkdir()

        proc_mock = AsyncMock()
        proc_mock.returncode = 0

        async def fake_communicate():
            (processed / "mixed.md").write_text(
                "---\nid: mixed\ntitle: Mixed\ntype: atom\n---\nbody"
            )
            return b"", b""

        proc_mock.communicate = fake_communicate

        with patch(
            "asyncio.create_subprocess_exec", return_value=proc_mock
        ) as mock_exec:
            await Gardener(vault_root=vault)._ingest_one(note)

        args = mock_exec.call_args[0]
        p_idx = list(args).index("-p")
        prompt = args[p_idx + 1]
        assert "SecretAuthor" not in prompt, "frontmatter fields must not be in prompt"
        assert "distinctive body text" not in prompt, "body must not be in prompt"
        assert str(note) in prompt, "file path must still appear in prompt"

    @pytest.mark.asyncio
    async def test_prompt_handles_path_with_spaces(self, tmp_path):
        """A vault path that contains spaces must be passed verbatim into the prompt.

        Paths with spaces are valid on Linux. The prompt must preserve the full
        path including spaces so Claude's Read tool can open it.
        """
        vault = tmp_path / "my vault"
        vault.mkdir()
        note = vault / "a note with spaces.md"
        note.write_text("---\ntitle: Spaced\n---\nbody")
        processed = vault / "_processed"
        processed.mkdir()

        proc_mock = AsyncMock()
        proc_mock.returncode = 0

        async def fake_communicate():
            (processed / "spaced.md").write_text(
                "---\nid: spaced\ntitle: Spaced\ntype: atom\n---\nbody"
            )
            return b"", b""

        proc_mock.communicate = fake_communicate

        with patch(
            "asyncio.create_subprocess_exec", return_value=proc_mock
        ) as mock_exec:
            await Gardener(vault_root=vault)._ingest_one(note)

        args = mock_exec.call_args[0]
        p_idx = list(args).index("-p")
        prompt = args[p_idx + 1]
        assert str(note) in prompt, "path with spaces must appear verbatim in prompt"

    @pytest.mark.asyncio
    async def test_prompt_raw_file_path_matches_exact_path_argument(self, tmp_path):
        """The raw_file_path embedded in the prompt must be str(path) exactly.

        _ingest_one() is called with an absolute Path object. The prompt must
        contain exactly str(path) — not a relative path, not path.stem, not
        something derived from the RawInput row.
        """
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "exact.md"
        note.write_text("---\ntitle: Exact\n---\nbody")
        processed = vault / "_processed"
        processed.mkdir()

        proc_mock = AsyncMock()
        proc_mock.returncode = 0

        async def fake_communicate():
            (processed / "exact.md").write_text(
                "---\nid: exact\ntitle: Exact\ntype: atom\n---\nbody"
            )
            return b"", b""

        proc_mock.communicate = fake_communicate

        with patch(
            "asyncio.create_subprocess_exec", return_value=proc_mock
        ) as mock_exec:
            await Gardener(vault_root=vault)._ingest_one(note)

        args = mock_exec.call_args[0]
        p_idx = list(args).index("-p")
        prompt = args[p_idx + 1]
        # str(note) is the exact absolute path passed to _ingest_one
        assert str(note) in prompt
        # Sanity: relative path ("exact.md") alone being present does NOT
        # satisfy the requirement — only str(note) (absolute) counts.
        assert str(vault) in prompt, (
            "vault parent must be present confirming absolute path"
        )

    @pytest.mark.asyncio
    async def test_spawns_claude_with_correct_flags(self, tmp_path):
        """_ingest_one spawns claude with --dangerously-skip-permissions and cwd=vault_root."""
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "test.md"
        note.write_text("# Hello\nsome content")
        processed = vault / "_processed"
        processed.mkdir()

        proc_mock = AsyncMock()
        proc_mock.returncode = 0

        async def fake_communicate():
            (processed / "hello.md").write_text(
                "---\nid: hello\ntitle: Hello\ntype: atom\n---\nbody"
            )
            return b"", b""

        proc_mock.communicate = fake_communicate

        with patch(
            "asyncio.create_subprocess_exec", return_value=proc_mock
        ) as mock_exec:
            await Gardener(vault_root=vault)._ingest_one(note)

        args = mock_exec.call_args[0]
        kwargs = mock_exec.call_args[1]
        assert args[0] == "claude"
        assert "--print" in args
        assert "--dangerously-skip-permissions" in args
        assert "--allowedTools" in args
        allowed_idx = list(args).index("--allowedTools")
        allowed_tools = args[allowed_idx + 1]
        assert "Bash" in allowed_tools
        assert "Write" in allowed_tools
        assert "WebSearch" in allowed_tools
        assert "WebFetch" in allowed_tools
        assert kwargs.get("cwd") == vault
        assert kwargs.get("env", {}).get("HOME") == "/tmp"

    @pytest.mark.asyncio
    async def test_raw_file_stays_in_place_after_decomposition(self, tmp_path):
        """Raw file stays in _raw/ after successful decomposition (no soft delete)."""
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "test.md"
        note.write_text("# Hello\ncontent")
        processed = vault / "_processed"
        processed.mkdir()

        proc_mock = AsyncMock()
        proc_mock.returncode = 0

        async def fake_communicate():
            (processed / "hello.md").write_text(
                "---\nid: hello\ntitle: Hello\ntype: atom\n---\nbody"
            )
            return b"", b""

        proc_mock.communicate = fake_communicate

        with patch("asyncio.create_subprocess_exec", return_value=proc_mock):
            await Gardener(vault_root=vault)._ingest_one(note)

        # Raw file stays in place (no soft delete to _deleted_with_ttl/)
        assert note.exists()

    @pytest.mark.asyncio
    async def test_leaves_raw_when_no_notes_created(self, tmp_path):
        """Raw file stays if claude exits 0 but writes no notes."""
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "test.md"
        note.write_text("# Hello\ncontent")

        proc_mock = AsyncMock()
        proc_mock.returncode = 0
        proc_mock.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=proc_mock):
            await Gardener(vault_root=vault)._ingest_one(note)

        assert note.exists()

    @pytest.mark.asyncio
    async def test_passes_home_tmp_in_env(self, tmp_path):
        """_ingest_one passes env kwarg with HOME=/tmp and inherits os.environ keys."""
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "test.md"
        note.write_text("# Hello\nsome content")
        processed = vault / "_processed"
        processed.mkdir()

        proc_mock = AsyncMock()
        proc_mock.returncode = 0

        async def fake_communicate():
            (processed / "hello.md").write_text(
                "---\nid: hello\ntitle: Hello\ntype: atom\n---\nbody"
            )
            return b"", b""

        proc_mock.communicate = fake_communicate

        with patch(
            "asyncio.create_subprocess_exec", return_value=proc_mock
        ) as mock_exec:
            await Gardener(vault_root=vault)._ingest_one(note)

        kwargs = mock_exec.call_args[1]
        env = kwargs.get("env")
        assert env is not None, "env kwarg must be passed to create_subprocess_exec"
        assert env.get("HOME") == "/tmp"
        # os.environ keys must be inherited (spot-check PATH which is always set)
        assert "PATH" in env
        assert env.get("PATH") == os.environ.get("PATH")

    @pytest.mark.asyncio
    async def test_logs_stdout_when_no_notes_created(self, tmp_path, caplog):
        """When claude exits 0 but creates no notes, the warning log includes stdout content."""
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "test.md"
        note.write_text("# Hello\ncontent")

        proc_mock = AsyncMock()
        proc_mock.returncode = 0
        proc_mock.communicate = AsyncMock(return_value=(b"some debug output", b""))

        with caplog.at_level(logging.WARNING, logger="monolith.knowledge.gardener"):
            with patch("asyncio.create_subprocess_exec", return_value=proc_mock):
                await Gardener(vault_root=vault)._ingest_one(note)

        assert note.exists()
        assert "some debug output" in caplog.text

    @pytest.mark.asyncio
    async def test_stdout_included_in_no_notes_warning(self, tmp_path, caplog):
        """When claude exits 0 but creates no notes, stdout is included in warning log."""
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "test.md"
        note.write_text("# Hello\ncontent")

        proc_mock = AsyncMock()
        proc_mock.returncode = 0
        proc_mock.communicate = AsyncMock(
            return_value=(b"I refused to create files.", b"")
        )

        with caplog.at_level(logging.WARNING, logger="monolith.knowledge.gardener"):
            with patch("asyncio.create_subprocess_exec", return_value=proc_mock):
                await Gardener(vault_root=vault)._ingest_one(note)

        assert note.exists()
        assert "I refused to create files." in caplog.text

    @pytest.mark.asyncio
    async def test_stdout_truncated_at_500_chars_in_no_notes_warning(
        self, tmp_path, caplog
    ):
        """stdout is truncated to 500 chars in the no-notes warning to avoid huge logs."""
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "test.md"
        note.write_text("# Hello\ncontent")

        long_stdout = b"x" * 600  # Exceeds the 500-char cap

        proc_mock = AsyncMock()
        proc_mock.returncode = 0
        proc_mock.communicate = AsyncMock(return_value=(long_stdout, b""))

        with caplog.at_level(logging.WARNING, logger="monolith.knowledge.gardener"):
            with patch("asyncio.create_subprocess_exec", return_value=proc_mock):
                await Gardener(vault_root=vault)._ingest_one(note)

        # The full 600-char string must NOT appear; only the first 500 chars are logged
        assert "x" * 501 not in caplog.text
        assert "x" * 500 in caplog.text

    @pytest.mark.asyncio
    async def test_raises_on_nonzero_exit(self, tmp_path):
        """RuntimeError is raised when claude exits with non-zero status."""
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "test.md"
        note.write_text("# Hello\ncontent")

        proc_mock = AsyncMock()
        proc_mock.returncode = 1
        proc_mock.communicate = AsyncMock(return_value=(b"", b"auth error"))

        with patch("asyncio.create_subprocess_exec", return_value=proc_mock):
            with pytest.raises(RuntimeError, match="claude exited 1"):
                await Gardener(vault_root=vault)._ingest_one(note)

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self, tmp_path):
        """RuntimeError is raised and subprocess killed when wait_for times out."""
        import asyncio as _asyncio

        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "test.md"
        note.write_text("# Hello\ncontent")

        proc_mock = MagicMock()
        proc_mock.returncode = None
        proc_mock.kill = MagicMock()
        proc_mock.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=proc_mock):
            with patch("asyncio.wait_for", side_effect=_asyncio.TimeoutError):
                with pytest.raises(RuntimeError, match="timed out"):
                    await Gardener(vault_root=vault)._ingest_one(note)

        proc_mock.kill.assert_called_once()
        proc_mock.wait.assert_awaited_once()


class TestSlugify:
    def test_ascii_text(self):
        assert _slugify("Hello World") == "hello-world"

    def test_unicode_nfkd_strips_accents(self):
        assert _slugify("Héllo") == "hello"

    def test_empty_string_returns_note(self):
        assert _slugify("") == "note"

    def test_multiple_special_chars_collapse_to_single_hyphen(self):
        assert _slugify("Hello!! World") == "hello-world"


class TestSplitFrontmatter:
    def test_valid_frontmatter_splits_correctly(self):
        raw = "---\ntitle: Test\n---\nBody"
        meta, body = _split_frontmatter(raw)
        assert meta == {"title": "Test"}
        assert body == "Body"

    def test_no_frontmatter_returns_empty_dict_and_raw(self):
        raw = "No frontmatter"
        meta, body = _split_frontmatter(raw)
        assert meta == {}
        assert body == raw

    def test_unclosed_frontmatter_returns_empty_dict_and_raw(self):
        raw = "---\ntitle: Test\n"
        meta, body = _split_frontmatter(raw)
        assert meta == {}
        assert body == raw

    def test_non_dict_yaml_returns_empty_dict_and_raw(self):
        raw = "---\n- item1\n- item2\n---\nBody"
        meta, body = _split_frontmatter(raw)
        assert meta == {}
        assert body == raw

    def test_invalid_yaml_returns_empty_dict_and_raw(self):
        raw = "---\n: invalid: yaml:\n---\nBody"
        meta, body = _split_frontmatter(raw)
        assert meta == {}
        assert body == raw

    def test_title_with_unquoted_colon_parses(self):
        raw = "---\ntitle: Concept: Important Idea\ntype: atom\n---\nBody"
        meta, body = _split_frontmatter(raw)
        assert meta == {"title": "Concept: Important Idea", "type": "atom"}
        assert body == "Body"


class TestRunFailurePath:
    @pytest.mark.asyncio
    async def test_continues_after_mid_run_failure(self, tmp_path, session):
        """run() continues processing files even when one raises an exception."""
        for name in ("after.md", "before.md", "middle.md"):
            _write(tmp_path, name, f"---\ntitle: {name}\n---\nBody.")

        gardener = Gardener(vault_root=tmp_path, session=session)
        calls: list[Path] = []

        async def fake_ingest(path: Path) -> None:
            calls.append(path)
            if "middle" in path.name:
                raise RuntimeError("simulated ingest failure")

        gardener._ingest_one = fake_ingest  # type: ignore[method-assign]
        stats = await gardener.run()

        assert len(calls) == 3
        assert stats.ingested == 2
        assert stats.failed == 1


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


class TestGardenerSkipsAlreadyProcessedRaws:
    def test_raws_with_current_version_provenance_are_skipped(self, tmp_path, session):
        from knowledge.gardener import GARDENER_VERSION
        from knowledge.models import AtomRawProvenance, RawInput

        raw = RawInput(
            raw_id="r1",
            path="_raw/2026/04/09/r1-n.md",
            source="vault-drop",
            content="Body.",
            content_hash="r1",
        )
        session.add(raw)
        session.flush()
        session.add(
            AtomRawProvenance(
                raw_fk=raw.id,
                gardener_version=GARDENER_VERSION,
            )
        )
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)
        to_process = gardener._raws_needing_decomposition()
        assert [r.raw_id for r in to_process] == []

    def test_grandfathered_sentinel_blocks_decomposition(self, tmp_path, session):
        from knowledge.models import AtomRawProvenance, RawInput

        raw = RawInput(
            raw_id="r2",
            path="_raw/grandfathered/r2-n.md",
            source="grandfathered",
            content="Body.",
            content_hash="r2",
        )
        session.add(raw)
        session.flush()
        session.add(
            AtomRawProvenance(
                raw_fk=raw.id,
                gardener_version="pre-migration",
            )
        )
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)
        assert gardener._raws_needing_decomposition() == []

    def test_new_raw_is_surfaced(self, tmp_path, session):
        from knowledge.models import RawInput

        raw = RawInput(
            raw_id="r3",
            path="_raw/2026/04/09/r3-n.md",
            source="vault-drop",
            content="Body.",
            content_hash="r3",
        )
        session.add(raw)
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)
        surfaced = gardener._raws_needing_decomposition()
        assert [r.raw_id for r in surfaced] == ["r3"]


class TestPrioritizedDecomposition:
    def test_fresh_raws_before_retriable_failed(self, tmp_path, session):
        """Fresh raws appear before retriable failed raws in the returned list."""
        from knowledge.gardener import GARDENER_VERSION
        from knowledge.models import AtomRawProvenance, RawInput

        # A failed raw with retry_count=1 (retriable)
        failed_raw = RawInput(
            raw_id="r-failed",
            path="_raw/2026/04/09/r-failed.md",
            source="vault-drop",
            content="Body.",
            content_hash="rf",
        )
        session.add(failed_raw)
        session.flush()
        session.add(
            AtomRawProvenance(
                raw_fk=failed_raw.id,
                derived_note_id="failed",
                gardener_version=GARDENER_VERSION,
                error="timeout",
                retry_count=1,
            )
        )
        # A fresh raw with no provenance
        fresh_raw = RawInput(
            raw_id="r-fresh",
            path="_raw/2026/04/09/r-fresh.md",
            source="vault-drop",
            content="Body.",
            content_hash="rfr",
        )
        session.add(fresh_raw)
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)
        result = gardener._raws_needing_decomposition()
        ids = [r.raw_id for r in result]
        assert ids == ["r-fresh", "r-failed"]

    def test_exhausted_raws_excluded(self, tmp_path, session):
        """A raw with retry_count >= _MAX_RETRIES is not returned."""
        from knowledge.gardener import GARDENER_VERSION
        from knowledge.models import AtomRawProvenance, RawInput

        raw = RawInput(
            raw_id="r-exhausted",
            path="_raw/2026/04/09/r-exhausted.md",
            source="vault-drop",
            content="Body.",
            content_hash="re",
        )
        session.add(raw)
        session.flush()
        session.add(
            AtomRawProvenance(
                raw_fk=raw.id,
                derived_note_id="failed",
                gardener_version=GARDENER_VERSION,
                error="timeout",
                retry_count=3,
            )
        )
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)
        result = gardener._raws_needing_decomposition()
        assert [r.raw_id for r in result] == []


class TestIngestOneRecordsPendingProvenance:
    @pytest.mark.asyncio
    async def test_inserts_pending_provenance_for_new_files(self, tmp_path, session):
        """After claude produces atoms, the gardener records
        pending atom_raw_provenance rows keyed by derived_note_id."""
        from knowledge.gardener import GARDENER_VERSION
        from knowledge.models import AtomRawProvenance, RawInput

        (tmp_path / "_raw" / "2026" / "04" / "09").mkdir(parents=True)
        raw_rel_path = "_raw/2026/04/09/r1-n.md"
        (tmp_path / raw_rel_path).write_text("Body.", encoding="utf-8")
        (tmp_path / "_processed").mkdir()

        raw = RawInput(
            raw_id="r1",
            path=raw_rel_path,
            source="vault-drop",
            content="Body.",
            content_hash="r1",
        )
        session.add(raw)
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)

        async def fake_subprocess(prompt: str) -> None:
            (tmp_path / "_processed" / "hello.md").write_text(
                "---\nid: hello\ntitle: Hello\ntype: atom\n---\nBody.\n",
                encoding="utf-8",
            )

        gardener._run_claude_subprocess = fake_subprocess  # type: ignore[method-assign]

        await gardener._ingest_one(tmp_path / raw_rel_path)

        rows = session.exec(select(AtomRawProvenance)).all()
        assert len(rows) == 1
        assert rows[0].raw_fk == raw.id
        assert rows[0].atom_fk is None
        assert rows[0].derived_note_id == "hello"
        assert rows[0].gardener_version == GARDENER_VERSION


class TestIngestOneProjectsSourceTier:
    """The gardener stamps ``source_tier`` onto every atom file derived from a
    ``type: research`` raw, where the tier is computed from the count of
    successfully-fetched ``web_fetch`` URLs in the raw's ``sources`` list:

    - 0 web_fetch sources → ``personal`` (vault-grounded only)
    - 1 web_fetch source  → ``direct``   (single primary source)
    - 2+ web_fetch sources → ``research`` (cross-source synthesis)

    Non-research raws are not touched (backwards compat with chat/journal/etc.).
    """

    @staticmethod
    def _seed_research_raw(
        tmp_path: Path,
        session,
        *,
        raw_id: str,
        raw_rel_path: str,
        sources_yaml: str,
    ) -> None:
        """Write a research raw under _processed/raws/research/ and seed its row."""
        from knowledge.models import RawInput

        raw_text = (
            "---\n"
            f"id: {raw_id}\n"
            f'title: "Research: {raw_id}"\n'
            "type: research\n"
            "source: research\n"
            f"{sources_yaml}"
            "---\n"
            "Body.\n"
        )
        full = tmp_path / raw_rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(raw_text, encoding="utf-8")
        (tmp_path / "_processed").mkdir(exist_ok=True)

        raw = RawInput(
            raw_id=raw_id,
            path=raw_rel_path,
            source="research",
            content=raw_text,
            content_hash=raw_id,
        )
        session.add(raw)
        session.commit()

    @staticmethod
    def _atom_frontmatter(atom_path: Path) -> dict:
        import yaml as _yaml

        text = atom_path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), text
        parts = text.split("---\n", 2)
        return _yaml.safe_load(parts[1]) or {}

    @pytest.mark.asyncio
    async def test_personal_when_no_web_fetch(self, tmp_path, session):
        """0 web_fetch sources → source_tier='personal'."""
        raw_rel = "_processed/raws/research/r1-topic.md"
        self._seed_research_raw(
            tmp_path,
            session,
            raw_id="r1",
            raw_rel_path=raw_rel,
            sources_yaml=(
                "sources:\n"
                "  - tool: search_knowledge\n"
                '    query: "topic"\n'
                "    note_ids: [a, b]\n"
            ),
        )

        gardener = Gardener(vault_root=tmp_path, session=session)
        atom_path = tmp_path / "_processed" / "topic.md"

        async def fake_subprocess(prompt: str) -> None:
            atom_path.write_text(
                "---\nid: topic\ntitle: Topic\ntype: atom\n---\nBody.\n",
                encoding="utf-8",
            )

        gardener._run_claude_subprocess = fake_subprocess  # type: ignore[method-assign]

        await gardener._ingest_one(tmp_path / raw_rel)

        fm = self._atom_frontmatter(atom_path)
        assert fm.get("source_tier") == "personal"

    @pytest.mark.asyncio
    async def test_direct_with_one_web_fetch(self, tmp_path, session):
        """Exactly one web_fetch source → source_tier='direct'."""
        raw_rel = "_processed/raws/research/r2-topic.md"
        self._seed_research_raw(
            tmp_path,
            session,
            raw_id="r2",
            raw_rel_path=raw_rel,
            sources_yaml=(
                "sources:\n"
                "  - tool: search_knowledge\n"
                '    query: "topic"\n'
                "    note_ids: [a]\n"
                "  - tool: web_fetch\n"
                "    url: https://example.com/a\n"
            ),
        )

        gardener = Gardener(vault_root=tmp_path, session=session)
        atom_path = tmp_path / "_processed" / "topic.md"

        async def fake_subprocess(prompt: str) -> None:
            atom_path.write_text(
                "---\nid: topic\ntitle: Topic\ntype: atom\n---\nBody.\n",
                encoding="utf-8",
            )

        gardener._run_claude_subprocess = fake_subprocess  # type: ignore[method-assign]

        await gardener._ingest_one(tmp_path / raw_rel)

        fm = self._atom_frontmatter(atom_path)
        assert fm.get("source_tier") == "direct"

    @pytest.mark.asyncio
    async def test_research_with_multiple_web_fetch(self, tmp_path, session):
        """2+ web_fetch sources → source_tier='research' (cross-source synthesis).

        A web_fetch entry whose url is null (skipped fetch) does not count
        toward the tier — only successfully-fetched URLs contribute.
        """
        raw_rel = "_processed/raws/research/r3-topic.md"
        self._seed_research_raw(
            tmp_path,
            session,
            raw_id="r3",
            raw_rel_path=raw_rel,
            sources_yaml=(
                "sources:\n"
                "  - tool: web_fetch\n"
                "    url: https://example.com/a\n"
                "  - tool: web_fetch\n"
                "    url: https://example.com/b\n"
                "  - tool: web_fetch\n"
                "    url: null\n"
            ),
        )

        gardener = Gardener(vault_root=tmp_path, session=session)
        atom_path = tmp_path / "_processed" / "topic.md"

        async def fake_subprocess(prompt: str) -> None:
            atom_path.write_text(
                "---\nid: topic\ntitle: Topic\ntype: atom\n---\nBody.\n",
                encoding="utf-8",
            )

        gardener._run_claude_subprocess = fake_subprocess  # type: ignore[method-assign]

        await gardener._ingest_one(tmp_path / raw_rel)

        fm = self._atom_frontmatter(atom_path)
        assert fm.get("source_tier") == "research"

    @pytest.mark.asyncio
    async def test_does_not_project_for_non_research_raws(self, tmp_path, session):
        """Non-research raws (e.g. chat/journal/vault-drop) don't get
        source_tier projected — backwards compat with all existing flows."""
        from knowledge.models import RawInput

        raw_rel = "_raw/2026/04/09/r4-note.md"
        raw_text = '---\nid: r4\ntitle: "Plain Note"\ntype: atom\n---\nBody.\n'
        full = tmp_path / raw_rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(raw_text, encoding="utf-8")
        (tmp_path / "_processed").mkdir(exist_ok=True)

        session.add(
            RawInput(
                raw_id="r4",
                path=raw_rel,
                source="vault-drop",
                content=raw_text,
                content_hash="r4",
            )
        )
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)
        atom_path = tmp_path / "_processed" / "topic.md"

        async def fake_subprocess(prompt: str) -> None:
            atom_path.write_text(
                "---\nid: topic\ntitle: Topic\ntype: atom\n---\nBody.\n",
                encoding="utf-8",
            )

        gardener._run_claude_subprocess = fake_subprocess  # type: ignore[method-assign]

        await gardener._ingest_one(tmp_path / raw_rel)

        fm = self._atom_frontmatter(atom_path)
        assert "source_tier" not in fm

    @pytest.mark.asyncio
    async def test_projection_is_idempotent(self, tmp_path, session):
        """Re-running the gardener on the same raw does not rewrite an atom
        file whose source_tier already matches the computed tier."""
        raw_rel = "_processed/raws/research/r5-topic.md"
        self._seed_research_raw(
            tmp_path,
            session,
            raw_id="r5",
            raw_rel_path=raw_rel,
            sources_yaml=(
                "sources:\n  - tool: web_fetch\n    url: https://example.com/a\n"
            ),
        )

        atom_path = tmp_path / "_processed" / "topic.md"
        atom_path.parent.mkdir(parents=True, exist_ok=True)
        # Pre-existing atom with the correct tier already projected.
        atom_path.write_text(
            "---\n"
            "id: topic\n"
            "title: Topic\n"
            "type: atom\n"
            "source_tier: direct\n"
            "---\n"
            "Body.\n",
            encoding="utf-8",
        )

        # Direct call to the projector — it should not rewrite when the value
        # is already current.
        before_mtime = atom_path.stat().st_mtime_ns
        before_text = atom_path.read_text(encoding="utf-8")
        Gardener._project_source_tier_onto_atom(atom_path, "direct")
        after_text = atom_path.read_text(encoding="utf-8")
        after_mtime = atom_path.stat().st_mtime_ns

        assert before_text == after_text
        assert before_mtime == after_mtime


class TestIngestOneNoNoteSentinel:
    @pytest.mark.asyncio
    async def test_records_sentinel_when_no_notes_produced(self, tmp_path, session):
        """When claude exits 0 but creates no new files, a sentinel
        provenance row prevents the raw from being reprocessed."""
        from knowledge.gardener import GARDENER_VERSION
        from knowledge.models import AtomRawProvenance, RawInput

        (tmp_path / "_raw" / "2026" / "04" / "09").mkdir(parents=True)
        raw_rel_path = "_raw/2026/04/09/r1-n.md"
        (tmp_path / raw_rel_path).write_text("Body.", encoding="utf-8")

        raw = RawInput(
            raw_id="r1",
            path=raw_rel_path,
            source="vault-drop",
            content="Body.",
            content_hash="r1",
        )
        session.add(raw)
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)

        async def fake_subprocess(prompt: str) -> None:
            pass  # produces no files

        gardener._run_claude_subprocess = fake_subprocess  # type: ignore[method-assign]

        await gardener._ingest_one(tmp_path / raw_rel_path)

        rows = session.exec(select(AtomRawProvenance)).all()
        assert len(rows) == 1
        assert rows[0].raw_fk == raw.id
        assert rows[0].derived_note_id == "no-new-notes"
        assert rows[0].gardener_version == GARDENER_VERSION


class TestGardenerRunPhases:
    @pytest.mark.asyncio
    async def test_run_invokes_move_then_reconcile_then_decompose(
        self, tmp_path, session
    ):
        from knowledge.models import RawInput

        (tmp_path / "inbox").mkdir()
        (tmp_path / "inbox" / "note.md").write_text(
            "---\ntitle: Note\n---\nBody.", encoding="utf-8"
        )

        gardener = Gardener(vault_root=tmp_path, session=session)
        gardener._ingest_one = AsyncMock()

        stats = await gardener.run()

        assert not (tmp_path / "inbox" / "note.md").exists()
        raw_files = list((tmp_path / "_raw").rglob("*.md"))
        assert len(raw_files) == 1
        raws_in_db = session.exec(select(RawInput)).all()
        assert len(raws_in_db) == 1
        assert gardener._ingest_one.call_count == 1
        assert stats.ingested == 1


class TestGardenerInit:
    """Verify that Gardener.__init__() stores all constructor parameters correctly."""

    def test_stores_vault_root_as_path(self, tmp_path):
        """vault_root is stored as a Path object."""
        gardener = Gardener(vault_root=tmp_path)
        assert gardener.vault_root == Path(tmp_path)

    def test_vault_root_string_is_coerced_to_path(self, tmp_path):
        """A string vault_root is coerced to a Path by the constructor."""
        gardener = Gardener(vault_root=str(tmp_path))
        assert isinstance(gardener.vault_root, Path)
        assert gardener.vault_root == Path(tmp_path)

    def test_stores_max_files_per_run(self, tmp_path):
        """max_files_per_run is stored as provided."""
        gardener = Gardener(vault_root=tmp_path, max_files_per_run=5)
        assert gardener.max_files_per_run == 5

    def test_default_max_files_per_run(self, tmp_path):
        """max_files_per_run defaults to 10 when not provided."""
        gardener = Gardener(vault_root=tmp_path)
        assert gardener.max_files_per_run == 10

    def test_stores_claude_bin(self, tmp_path):
        """claude_bin is stored as provided."""
        gardener = Gardener(vault_root=tmp_path, claude_bin="/usr/local/bin/claude")
        assert gardener.claude_bin == "/usr/local/bin/claude"

    def test_default_claude_bin(self, tmp_path):
        """claude_bin defaults to 'claude' when not provided."""
        gardener = Gardener(vault_root=tmp_path)
        assert gardener.claude_bin == "claude"

    def test_stores_session(self, tmp_path, session):
        """session is stored as provided."""
        gardener = Gardener(vault_root=tmp_path, session=session)
        assert gardener.session is session

    def test_default_session_is_none(self, tmp_path):
        """session defaults to None when not provided."""
        gardener = Gardener(vault_root=tmp_path)
        assert gardener.session is None

    def test_processed_root_derived_from_vault_root(self, tmp_path):
        """processed_root is derived as vault_root/_processed."""
        gardener = Gardener(vault_root=tmp_path)
        assert gardener.processed_root == tmp_path / "_processed"

    def test_processed_root_uses_resolved_vault_root(self, tmp_path):
        """processed_root is always relative to the stored vault_root Path."""
        vault = tmp_path / "my-vault"
        vault.mkdir()
        gardener = Gardener(vault_root=vault)
        assert gardener.processed_root == vault / "_processed"


class TestAtomRawProvenanceDeadLetterFields:
    def test_error_field_defaults_to_none(self, session):
        from knowledge.models import AtomRawProvenance, RawInput

        raw = RawInput(
            raw_id="r1",
            path="_raw/r1.md",
            source="vault-drop",
            content="Body.",
            content_hash="r1",
        )
        session.add(raw)
        session.flush()
        prov = AtomRawProvenance(
            raw_fk=raw.id,
            gardener_version="v1",
        )
        session.add(prov)
        session.commit()
        assert prov.error is None

    def test_retry_count_defaults_to_zero(self, session):
        from knowledge.models import AtomRawProvenance, RawInput

        raw = RawInput(
            raw_id="r2",
            path="_raw/r2.md",
            source="vault-drop",
            content="Body.",
            content_hash="r2",
        )
        session.add(raw)
        session.flush()
        prov = AtomRawProvenance(
            raw_fk=raw.id,
            gardener_version="v1",
        )
        session.add(prov)
        session.commit()
        assert prov.retry_count == 0

    def test_accepts_error_and_retry_count(self, session):
        from knowledge.models import AtomRawProvenance, RawInput

        raw = RawInput(
            raw_id="r3",
            path="_raw/r3.md",
            source="vault-drop",
            content="Body.",
            content_hash="r3",
        )
        session.add(raw)
        session.flush()
        prov = AtomRawProvenance(
            raw_fk=raw.id,
            gardener_version="v1",
            error="timeout after 300s",
            retry_count=2,
        )
        session.add(prov)
        session.commit()
        assert prov.error == "timeout after 300s"
        assert prov.retry_count == 2


class TestResolvePendingProvenance:
    def test_resolves_note_id_to_atom_fk(self, tmp_path, session):
        from knowledge.gardener import GARDENER_VERSION
        from knowledge.models import AtomRawProvenance, Note, RawInput

        raw = RawInput(
            raw_id="r1",
            path="_raw/2026/04/09/r1.md",
            source="vault-drop",
            content="Body.",
            content_hash="r1",
        )
        note = Note(
            note_id="hello",
            path="_processed/atoms/hello.md",
            title="Hello",
            content_hash="h1",
            type="atom",
        )
        session.add_all([raw, note])
        session.flush()
        session.add(
            AtomRawProvenance(
                raw_fk=raw.id,
                derived_note_id="hello",
                gardener_version=GARDENER_VERSION,
            )
        )
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)
        resolved = gardener._resolve_pending_provenance()
        session.commit()

        assert resolved == 1
        rows = session.exec(select(AtomRawProvenance)).all()
        assert len(rows) == 1
        assert rows[0].atom_fk == note.id
        assert rows[0].derived_note_id is None

    def test_leaves_unresolved_when_note_missing(self, tmp_path, session):
        from knowledge.gardener import GARDENER_VERSION
        from knowledge.models import AtomRawProvenance, RawInput

        raw = RawInput(
            raw_id="r1",
            path="_raw/2026/04/09/r1.md",
            source="vault-drop",
            content="Body.",
            content_hash="r1",
        )
        session.add(raw)
        session.flush()
        session.add(
            AtomRawProvenance(
                raw_fk=raw.id,
                derived_note_id="ghost",
                gardener_version=GARDENER_VERSION,
            )
        )
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)
        assert gardener._resolve_pending_provenance() == 0
        row = session.exec(select(AtomRawProvenance)).first()
        assert row.atom_fk is None
        assert row.derived_note_id == "ghost"


class TestPromptTemplateInstructions:
    """Regression guards for critical instructions in _CLAUDE_PROMPT_HEADER.

    Each test pins a specific instruction keyword so that accidental deletions
    or edits are caught immediately by CI rather than surfacing as silent
    model-behaviour regressions.
    """

    def test_prompt_includes_no_title_prefix_instruction(self):
        """Guard against regression where category-label prefixes reappear in titles.

        Commit 7e6b7a20 added the instruction 'Do NOT prefix titles with
        category labels' to prevent Claude from emitting titles like
        '(Book) Staff Engineer's Path'. This assertion ensures the instruction
        is never accidentally removed.
        """
        assert "Do NOT prefix titles with category labels" in _CLAUDE_PROMPT_HEADER

    def test_prompt_includes_filename_must_match_id_instruction(self):
        """Guard against regression where filenames diverge from their note id.

        Commit 7e6b7a20 added the instruction 'filename MUST be' to enforce
        that each written file is named exactly '<id>.md'. This assertion
        ensures the instruction is never accidentally removed.
        """
        assert "filename MUST be" in _CLAUDE_PROMPT_HEADER

    def test_prompt_header_says_a_raw_note_not_below(self):
        """Guard against reintroducing 'the raw note below' wording.

        Commit 856d785 changed the preamble from 'Decompose the raw note below'
        to 'Decompose a raw note' because the raw content is no longer inlined
        in the prompt. If someone reverts the wording, Claude will receive an
        instruction that contradicts how the prompt is now structured.
        """
        assert "Decompose a raw note" in _CLAUDE_PROMPT_HEADER
        assert "Decompose the raw note below" not in _CLAUDE_PROMPT_HEADER

    def test_prompt_step_1_instructs_read_tool_for_file_path(self):
        """Guard against step 1 regressing to the old keyword-search first step.

        Commit 856d785 made step 1 'Read the raw note from {raw_file_path}
        using the Read tool.' so Claude reads the file before searching. If the
        step is removed or reordered, Claude has no content to decompose.
        """
        assert "Read the raw note from" in _CLAUDE_PROMPT_HEADER
        assert "using the Read tool" in _CLAUDE_PROMPT_HEADER

    def test_prompt_header_contains_raw_file_path_placeholder(self):
        """Guard against the {raw_file_path} format placeholder being removed.

        _ingest_one() calls _CLAUDE_PROMPT_HEADER.format(raw_file_path=path, ...)
        so the placeholder must be present in the template or .format() will
        silently drop the path and Claude will have no file to read.
        """
        assert "{raw_file_path}" in _CLAUDE_PROMPT_HEADER

    def test_prompt_header_encourages_forward_wikilinks(self):
        """Guard against losing the body-wikilink guidance.

        Body wikilinks ([[...]] in prose) are the trigger for the gap →
        research → atom pipeline (store.py captures them as kind='link'
        which feeds gap_classifier). Without this section the gardener
        emits typed edges only, the graph stops growing organically, and
        the research pipeline starves for inputs. If this guidance is
        removed the loop breaks silently — there's no other test that
        would catch it.
        """
        assert "Linking to neighboring concepts" in _CLAUDE_PROMPT_HEADER
        assert "[[Concept Name]]" in _CLAUDE_PROMPT_HEADER
        assert "Linking to neighboring concepts" in _DISTILL_PROMPT


class TestRecordFailedProvenance:
    @pytest.mark.asyncio
    async def test_records_failed_provenance_on_exception(self, tmp_path, session):
        """When _run_claude_subprocess raises, a provenance row is created
        with derived_note_id='failed', retry_count=1, and error populated."""
        from knowledge.gardener import GARDENER_VERSION
        from knowledge.models import AtomRawProvenance, RawInput

        (tmp_path / "_raw" / "2026" / "04" / "09").mkdir(parents=True)
        raw_rel_path = "_raw/2026/04/09/r1-n.md"
        (tmp_path / raw_rel_path).write_text("Body.", encoding="utf-8")

        raw = RawInput(
            raw_id="r1",
            path=raw_rel_path,
            source="vault-drop",
            content="Body.",
            content_hash="r1",
        )
        session.add(raw)
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)

        async def failing_subprocess(prompt: str) -> None:
            raise RuntimeError("claude exited 1: auth error")

        gardener._run_claude_subprocess = failing_subprocess  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="auth error"):
            await gardener._ingest_one(tmp_path / raw_rel_path)

        rows = session.exec(select(AtomRawProvenance)).all()
        assert len(rows) == 1
        assert rows[0].raw_fk == raw.id
        assert rows[0].derived_note_id == "failed"
        assert rows[0].retry_count == 1
        assert "auth error" in rows[0].error
        assert rows[0].gardener_version == GARDENER_VERSION

    @pytest.mark.asyncio
    async def test_increments_retry_count_on_repeated_failure(self, tmp_path, session):
        """Pre-existing failed provenance with retry_count=1 becomes
        retry_count=2 after another failure."""
        from knowledge.gardener import GARDENER_VERSION
        from knowledge.models import AtomRawProvenance, RawInput

        (tmp_path / "_raw" / "2026" / "04" / "09").mkdir(parents=True)
        raw_rel_path = "_raw/2026/04/09/r1-n.md"
        (tmp_path / raw_rel_path).write_text("Body.", encoding="utf-8")

        raw = RawInput(
            raw_id="r1",
            path=raw_rel_path,
            source="vault-drop",
            content="Body.",
            content_hash="r1",
        )
        session.add(raw)
        session.flush()
        # Pre-existing failed provenance row
        session.add(
            AtomRawProvenance(
                raw_fk=raw.id,
                derived_note_id="failed",
                gardener_version=GARDENER_VERSION,
                error="previous error",
                retry_count=1,
            )
        )
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)

        async def failing_subprocess(prompt: str) -> None:
            raise RuntimeError("timeout after 300s")

        gardener._run_claude_subprocess = failing_subprocess  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="timeout"):
            await gardener._ingest_one(tmp_path / raw_rel_path)

        rows = session.exec(select(AtomRawProvenance)).all()
        assert len(rows) == 1
        assert rows[0].retry_count == 2
        assert "timeout" in rows[0].error
        assert rows[0].gardener_version == GARDENER_VERSION


class TestGardenerDedupesFrontmatterEachCycle:
    """The gardener cleans duplicate-key stub frontmatter on every cycle.

    Production accumulated ~600 stubs with duplicate ``status:`` lines because
    Claude's classifier ``Edit`` invocations appended new keys instead of
    replacing existing ones. The gardener runs ``dedupe_stub_frontmatter`` at
    the top of every ``run()`` cycle. Per-cycle execution is cheap because the
    helper's byte-comparison idempotency short-circuits on already-clean
    stubs — no rewrites, no mtime churn. Self-healing if duplicates ever
    re-emerge.
    """

    @pytest.mark.asyncio
    async def test_dedupes_on_first_run_and_is_no_op_on_second(self, tmp_path, session):
        """First run() cleans the stub; second run() leaves the file untouched.

        The second-run no-op comes from the helper's byte-comparison
        idempotency, not from any instance flag — the Gardener is
        reconstructed per scheduled tick, so flags wouldn't survive anyway.
        """
        stub_dir = tmp_path / "_researching"
        stub_dir.mkdir()
        bad_stub = stub_dir / "x.md"
        bad_stub.write_text(
            "---\nid: x\ntype: gap\nstatus: discovered\nstatus: classified\n---\n"
        )

        gardener = Gardener(vault_root=tmp_path, session=session)
        await gardener.run()

        text = bad_stub.read_text()
        assert text.count("status:") == 1, "first run cleans the stub"

        # Second run is a no-op via byte-comparison idempotency in the helper.
        mtime = bad_stub.stat().st_mtime_ns
        await gardener.run()
        assert bad_stub.stat().st_mtime_ns == mtime, (
            "second run does not touch the file"
        )


class TestGardenerGapDiscovery:
    """Gap discovery is wired into each garden cycle.

    Classification has been hoisted out of the gardener cycle and is now
    owned by the ``knowledge.classify-gaps`` scheduled job (see
    ``service.py``). The gardener only discovers — it must never call
    ``classify_gaps`` itself.
    """

    @pytest.mark.asyncio
    async def test_gardener_run_discovers_gaps_without_classifying(
        self, tmp_path, session, caplog
    ):
        """run() invokes discover_gaps and leaves the gap at
        state='discovered' for the classify-gaps scheduled job to pick up.
        No classifier-missing warning is emitted because the gardener no
        longer calls classify_gaps.
        """
        from knowledge.models import Gap, Note, NoteLink

        # Seed a Note with a body wikilink pointing at an unresolved target.
        src = Note(
            note_id="source-note",
            path="_processed/source-note.md",
            title="Source Note",
            content_hash="h-src",
            type="atom",
        )
        session.add(src)
        session.flush()
        session.add(
            NoteLink(
                src_note_fk=src.id,
                target_id="missing-concept",
                target_title="missing-concept",
                kind="link",
                edge_type=None,
            )
        )
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)
        # No raws in the empty vault, so no claude subprocess is spawned.
        with caplog.at_level(logging.WARNING, logger="knowledge.gaps"):
            stats = await gardener.run()

        assert stats.gaps_discovered == 1

        gap = session.exec(select(Gap)).one()
        assert gap.state == "discovered"
        assert gap.gap_class is None

        # The "no classifier wired" warning came from gaps.classify_gaps —
        # we no longer call it from the gardener cycle, so the warning must
        # not appear in cycle logs.
        assert not any(
            "gaps awaiting classification but no classifier is wired"
            in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_gardener_gap_failure_does_not_break_cycle(
        self, tmp_path, session, caplog
    ):
        """When discover_gaps raises, the gardener logs the exception and
        returns a zero discovered count rather than failing the whole cycle."""
        from knowledge.models import Note, NoteLink

        src = Note(
            note_id="source-note",
            path="_processed/source-note.md",
            title="Source Note",
            content_hash="h-src",
            type="atom",
        )
        session.add(src)
        session.flush()
        session.add(
            NoteLink(
                src_note_fk=src.id,
                target_id="missing-concept",
                target_title="missing-concept",
                kind="link",
                edge_type=None,
            )
        )
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)

        def boom(_session, _vault_root):
            raise RuntimeError("boom")

        with caplog.at_level(logging.ERROR, logger="monolith.knowledge.gardener"):
            with patch("knowledge.gaps.discover_gaps", side_effect=boom):
                stats = await gardener.run()

        assert stats.gaps_discovered == 0
        assert "discover_gaps failed" in caplog.text

    def test_discover_gaps_returns_zero_when_session_is_none(self, tmp_path):
        """Session-less gardeners (e.g. dry-run setups) skip the gap step cleanly."""
        gardener = Gardener(vault_root=tmp_path)  # no session
        assert gardener._discover_gaps() == 0

    @pytest.mark.asyncio
    async def test_gardener_does_not_invoke_classify_gaps(
        self, monkeypatch, tmp_path, session
    ):
        """Legacy classify_gaps stays out of the gardener cycle. The
        knowledge.classify-gaps scheduled job is the sole caller now.
        """
        import knowledge.gaps as gaps_module
        from knowledge.models import Note, NoteLink

        # Seed a real unresolved wikilink so discover_gaps has work to do —
        # we want to prove classify is NOT called even when gaps exist.
        src = Note(
            note_id="source-note",
            path="_processed/source-note.md",
            title="Source Note",
            content_hash="h-src",
            type="atom",
        )
        session.add(src)
        session.flush()
        session.add(
            NoteLink(
                src_note_fk=src.id,
                target_id="missing-concept",
                target_title="missing-concept",
                kind="link",
                edge_type=None,
            )
        )
        session.commit()

        calls: list[tuple[tuple, dict]] = []

        def tracker(*args, **kwargs):
            calls.append((args, kwargs))
            return 0

        monkeypatch.setattr(gaps_module, "classify_gaps", tracker)

        gardener = Gardener(vault_root=tmp_path, session=session)
        await gardener.run()

        assert calls == [], (
            f"gardener must not call classify_gaps; got {len(calls)} call(s)"
        )

    def test_garden_stats_has_no_gaps_classified_field(self):
        """gaps_classified is removed from GardenStats — the
        knowledge.classify-gaps scheduled job owns classification now.
        """
        import dataclasses

        from knowledge.gardener import GardenStats

        field_names = {f.name for f in dataclasses.fields(GardenStats)}
        assert "gaps_classified" not in field_names, (
            f"gaps_classified should be removed; got fields: {sorted(field_names)}"
        )
