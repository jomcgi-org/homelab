"""Tests for gen_env_readme.py (ADR agents/044).

The generator turns an apko lock file plus a hand-written notes file into the
/etc/environment.md baked into every Firecracker guest image, so these tests
pin the doc contract: one row per package name+version, deduped across archs,
sorted, with the notes content ahead of the table.
"""

import json
import sys

import pytest

from gen_env_readme import main

# A synthetic dual-arch lock: apko lists each package once per architecture
# under contents.packages, so zlib appears twice (x86_64 + aarch64) and must
# collapse to a single table row. The entry with no name/version exercises the
# "?" fallback.
LOCK = {
    "version": "v1",
    "config": {"checksum": "sha256-irrelevant"},
    "contents": {
        "packages": [
            {"name": "zlib", "version": "1.3.2-r3", "architecture": "x86_64"},
            {"name": "zlib", "version": "1.3.2-r3", "architecture": "aarch64"},
            {"name": "bash", "version": "5.3-r12", "architecture": "x86_64"},
            {"name": "bash", "version": "5.3-r12", "architecture": "aarch64"},
            {"architecture": "x86_64"},
        ]
    },
}

NOTES = "You are inside a disposable microVM.\nWrite scratch files under /tmp.\n"


@pytest.fixture
def output(tmp_path, monkeypatch, capsys):
    lock_path = tmp_path / "apko.lock.json"
    lock_path.write_text(json.dumps(LOCK))
    notes_path = tmp_path / "env-notes.md"
    notes_path.write_text(NOTES)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gen_env_readme",
            "--lock",
            str(lock_path),
            "--title",
            "test guest environment",
            "--notes",
            str(notes_path),
        ],
    )
    main()
    return capsys.readouterr().out


def test_per_arch_duplicates_collapse_to_one_row(output):
    assert output.count("| zlib | 1.3.2-r3 |") == 1
    assert output.count("| bash | 5.3-r12 |") == 1


def test_rows_are_sorted_markdown_table(output):
    lines = output.splitlines()
    header = lines.index("| Package | Version |")
    assert lines[header + 1] == "| ------- | ------- |"
    rows = lines[header + 2 :]
    assert rows == [
        "| ? | ? |",
        "| bash | 5.3-r12 |",
        "| zlib | 1.3.2-r3 |",
    ]


def test_notes_content_appears_before_table(output):
    assert output.startswith("# test guest environment\n")
    notes_pos = output.index("disposable microVM")
    table_pos = output.index("| Package | Version |")
    assert notes_pos < table_pos


def test_missing_name_and_version_fall_back_to_question_mark(output):
    assert "| ? | ? |" in output
