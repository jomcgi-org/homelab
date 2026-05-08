"""Tests for the LLM-driven Phase-2 visibility backfill."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from knowledge.tools.classify_visibility_backfill import (
    BackfillError,
    classify_one,
    run,
)


def _seed_note(p: Path, slug: str, visibility: str | None = None) -> Path:
    fm = f"---\nid: {slug}\ntitle: {slug}\n"
    if visibility is not None:
        fm += f"visibility: {visibility}\n"
    else:
        fm += "visibility:\n"
    fm += f"---\nbody for {slug}.\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(fm)
    return p


@pytest.fixture
def vault(tmp_path):
    _seed_note(tmp_path / "_processed/a.md", "a", visibility=None)
    _seed_note(tmp_path / "_processed/b.md", "b", visibility="public")
    _seed_note(tmp_path / "_processed/c.md", "c", visibility=None)
    return tmp_path


def test_refuses_report_path_inside_repo(vault):
    # Anchor the safety check to the vault itself so a report path
    # *inside* the vault triggers the refusal even though the vault is
    # under pytest's tmpdir (which sits outside the actual git root).
    with pytest.raises(BackfillError, match="outside the repo"):
        run(
            vault_root=vault,
            report=vault / "out.json",
            run_one_for_test=lambda body: ("private", "test"),
            _repo_root_for_test=vault,
        )


def _report_outside_repo(tmp_path: Path, name: str) -> Path:
    """Return a report path that is guaranteed outside the actual git repo.

    pytest's ``tmp_path`` resolves under the OS tmp dir (e.g.
    ``/private/var/folders/...``), which is outside the homelab worktree
    regardless of where pytest is invoked from — so the script's safety
    check accepts it without needing ``_repo_root_for_test``.
    """
    return tmp_path / name


def test_skips_already_labelled_notes(vault, tmp_path):
    calls = []

    def fake(body):
        calls.append(body)
        return "public", "ok"

    run(
        vault_root=vault,
        report=_report_outside_repo(tmp_path, "report-skip.json"),
        run_one_for_test=fake,
    )
    # b.md is already "public" — skip; only a + c go through classifier.
    assert len(calls) == 2


def test_writes_decision_back_to_frontmatter(vault, tmp_path):
    def fake(body):
        return "public", "looks public"

    run(
        vault_root=vault,
        report=_report_outside_repo(tmp_path, "report-write.json"),
        run_one_for_test=fake,
    )
    a = (vault / "_processed/a.md").read_text()
    assert "visibility: public" in a


def test_strict_json_parse_failure_leaves_file_unchanged(vault, tmp_path):
    def bad(body):
        raise BackfillError("invalid JSON")

    run(
        vault_root=vault,
        report=_report_outside_repo(tmp_path, "report-bad.json"),
        run_one_for_test=bad,
    )
    a = (vault / "_processed/a.md").read_text()
    # a.md still has the empty `visibility:` line untouched.
    assert "visibility:\n" in a or "visibility: \n" in a


def test_resumable_after_partial_run(vault, tmp_path):
    # First run: only handle one file (a → public via max_files=1).
    def fake_a(body):
        return "public", "ok"

    run(
        vault_root=vault,
        report=_report_outside_repo(tmp_path, "report-r1.json"),
        run_one_for_test=fake_a,
        max_files=1,
    )

    # Second run: c is the only remaining unlabelled note.
    seen = []

    def fake_c(body):
        seen.append(body)
        return "private", "ok"

    run(
        vault_root=vault,
        report=_report_outside_repo(tmp_path, "report-r2.json"),
        run_one_for_test=fake_c,
    )
    assert len(seen) == 1


@patch("knowledge.tools.classify_visibility_backfill.subprocess.run")
def test_classify_one_parses_strict_json(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(
            {"result": json.dumps({"visibility": "public", "rationale": "concept"})}
        ),
        stderr="",
    )
    decision, rationale = classify_one(body="body", title="t")
    assert decision == "public"
    assert rationale == "concept"


@patch("knowledge.tools.classify_visibility_backfill.subprocess.run")
def test_classify_one_rejects_unknown_decision(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(
            {"result": json.dumps({"visibility": "yellow", "rationale": "x"})}
        ),
        stderr="",
    )
    with pytest.raises(BackfillError):
        classify_one(body="body", title="t")
