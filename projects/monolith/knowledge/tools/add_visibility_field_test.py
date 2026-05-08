"""Tests for the mechanical Phase-1 visibility field injector."""

from pathlib import Path

from knowledge.tools.add_visibility_field import run


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_inserts_visibility_into_files_lacking_it(tmp_path):
    note = tmp_path / "_processed/foo.md"
    _write(note, "---\nid: foo\ntitle: Foo\n---\nbody\n")
    stats = run(vault_root=tmp_path, dirs=["_processed"])
    assert stats.added == 1
    assert "visibility:\n" in note.read_text()


def test_skips_files_already_with_visibility(tmp_path):
    note = tmp_path / "_processed/bar.md"
    _write(note, "---\nid: bar\ntitle: Bar\nvisibility: public\n---\nbody\n")
    before = note.read_text()
    stats = run(vault_root=tmp_path, dirs=["_processed"])
    assert stats.added == 0
    assert stats.already_set == 1
    assert note.read_text() == before  # byte-stable


def test_idempotent_second_run(tmp_path):
    note = tmp_path / "_processed/baz.md"
    _write(note, "---\nid: baz\ntitle: Baz\n---\nbody\n")
    run(vault_root=tmp_path, dirs=["_processed"])
    after_first = note.read_text()
    run(vault_root=tmp_path, dirs=["_processed"])
    assert note.read_text() == after_first


def test_skips_files_with_no_frontmatter(tmp_path):
    note = tmp_path / "_processed/raw.md"
    _write(note, "Just a body, no frontmatter.\n")
    stats = run(vault_root=tmp_path, dirs=["_processed"])
    assert stats.added == 0
    assert stats.parse_skipped == 1


def test_skips_files_with_unparseable_frontmatter(tmp_path):
    note = tmp_path / "_processed/broken.md"
    # Unclosed flow collection — sanitizer doesn't touch this; yaml raises.
    _write(note, "---\nfoo: [unclosed\n---\nbody\n")
    stats = run(vault_root=tmp_path, dirs=["_processed"])
    assert stats.parse_skipped == 1


def test_atomic_write_does_not_truncate_on_failure(tmp_path, monkeypatch):
    note = tmp_path / "_processed/atomic.md"
    _write(note, "---\nid: a\n---\nbody\n")

    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr("os.replace", boom)
    try:
        run(vault_root=tmp_path, dirs=["_processed"])
    except OSError:
        pass
    # Original file is intact
    assert "id: a" in note.read_text()
