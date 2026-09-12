"""Regression tests for the central monolith BUILD ratchet."""

from __future__ import annotations

import subprocess

import pytest

import central_build_ratchet as ratchet


LEGACY = """\
# gazelle:exclude auth
_BACKEND_SRCS = glob(
    [
        "auth/**/*.py",
        "**/*_test.py",
    ],
)

py_library(
    name = "pkg_auth",
    srcs = glob(["auth/**/*.py"]),
)
"""


def _assert_allowed(candidate: str) -> None:
    assert ratchet.new_findings(LEGACY, candidate) == []


def _assert_rejected(candidate: str, kind: str, value: str) -> None:
    additions = ratchet.new_findings(LEGACY, candidate)
    assert [(finding.kind, finding.value) for finding in additions] == [(kind, value)]


def test_unchanged_legacy_content_is_grandfathered():
    _assert_allowed(LEGACY)


def test_removing_legacy_content_is_allowed():
    _assert_allowed(
        LEGACY.replace("# gazelle:exclude auth\n", "").replace(
            '        "auth/**/*.py",\n', ""
        )
    )


def test_new_central_gazelle_exclude_is_rejected():
    _assert_rejected(
        LEGACY.replace(
            "# gazelle:exclude auth\n",
            "# gazelle:exclude auth\n# gazelle:exclude chat\n",
        ),
        "gazelle:exclude",
        "chat",
    )


def test_same_count_gazelle_exclude_replacement_is_rejected():
    _assert_rejected(
        LEGACY.replace("# gazelle:exclude auth", "# gazelle:exclude chat"),
        "gazelle:exclude",
        "chat",
    )


def test_duplicate_central_gazelle_exclude_is_rejected():
    _assert_rejected(
        LEGACY.replace(
            "# gazelle:exclude auth\n",
            "# gazelle:exclude auth\n# gazelle:exclude auth\n",
        ),
        "gazelle:exclude",
        "auth",
    )


@pytest.mark.parametrize(
    ("suffix", "value"),
    [("  # legacy", "chat  # legacy"), (" notes", "chat notes")],
)
def test_malformed_central_gazelle_exclude_is_rejected(suffix, value):
    _assert_rejected(
        LEGACY + f"# gazelle:exclude chat{suffix}\n",
        "gazelle:exclude",
        value,
    )


def test_new_central_package_glob_is_rejected():
    _assert_rejected(
        LEGACY.replace(
            '        "auth/**/*.py",\n',
            '        "auth/**/*.py",\n        "chat/**/*.py",\n',
        ),
        "package glob",
        "chat/**/*.py",
    )


def test_same_count_package_glob_replacement_is_rejected():
    _assert_rejected(
        LEGACY.replace('        "auth/**/*.py",\n', '        "chat/**/*.py",\n'),
        "package glob",
        "chat/**/*.py",
    )


def test_moving_a_grandfathered_glob_to_another_target_is_rejected():
    candidate = LEGACY.replace('        "auth/**/*.py",\n', "").replace(
        "\npy_library(",
        '\nfilegroup(name = "moved_auth", '
        'srcs = glob(["auth/**/*.py"]))\n\npy_library(',
    )
    _assert_rejected(candidate, "package glob", "auth/**/*.py")


def test_concatenated_package_glob_is_rejected():
    _assert_rejected(
        LEGACY + 'PACKAGE = "chat"\nfilegroup(srcs = glob([PACKAGE + "/**/*.py"]))\n',
        "package glob",
        "chat/**/*.py",
    )


def test_broad_globs_are_not_per_package_enumeration():
    _assert_allowed(LEGACY.replace('"**/*_test.py",', '"**/*.py",'))


def test_explicit_source_paths_are_outside_ratchet_scope():
    _assert_allowed(
        LEGACY
        + 'py_library(name = "pkg_chat", srcs = ["chat/main.py"])\n'
        + 'filegroup(name = "chat_files", srcs = ["chat/data.json"])\n'
    )


def test_legitimate_package_local_build_content_is_allowed():
    local_content = (
        "# gazelle:exclude generated.py\n"
        'py_library(name = "chat", srcs = glob(["*.py"]))\n'
    )
    assert ratchet.scan_central_build(local_content)
    assert ratchet.new_findings(LEGACY, LEGACY) == []


def test_missing_base_reference_fails_with_actionable_diagnostic(tmp_path, monkeypatch):
    def failed_git(*args, **_kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=128,
            stdout="",
            stderr="fatal: invalid object name 'origin/missing'",
        )

    monkeypatch.setattr(ratchet.subprocess, "run", failed_git)

    with pytest.raises(
        ratchet.RatchetError, match="cannot read base reference"
    ) as error:
        ratchet._git_text(tmp_path, "origin/missing", "base")

    assert "Fetch the pull request base branch" in str(error.value)
