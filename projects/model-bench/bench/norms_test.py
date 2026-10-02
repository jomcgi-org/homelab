import pytest  # noqa: F401

from bench import norms
from bench.norms import compute_norms, safe_norms


def _tree(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _pair(tmp_path, before, after):
    return _tree(tmp_path / "fx", before), _tree(tmp_path / "wd", after)


def test_clean_scoped_change_with_test_scores_one(tmp_path):
    fx, wd = _pair(
        tmp_path,
        {"pkg/mod.py": "def f():\n    return 1\n"},
        {
            "pkg/mod.py": "def f():\n    return 2\n",
            "pkg/mod_test.py": "from pkg.mod import f\n\n\ndef test_f():\n    assert f() == 2\n",
        },
    )
    n = compute_norms(
        fx, wd, target_files=["pkg/mod.py", "pkg/mod_test.py"], lint=False
    )
    assert n["files_changed"] == 2
    assert n["lines_added"] == 6 and n["lines_removed"] == 1
    assert n["files_outside_targets"] == 0
    assert n["test_added"] is True
    assert n["debug_leftovers"] == 0
    assert n["norms_score"] == 1.0


def test_debug_leftovers_and_scope_creep_are_penalised(tmp_path):
    fx, wd = _pair(
        tmp_path,
        {"a.py": "x = 1\n", "b.py": "y = 1\n"},
        {"a.py": "x = 2\nprint(x)\n# TODO tidy\n", "b.py": "y = 2\n"},
    )
    n = compute_norms(fx, wd, target_files=["a.py"], lint=False)
    assert n["debug_leftovers"] == 2
    assert n["files_outside_targets"] == 1
    assert n["test_added"] is False
    # scope 0.5*0.25 + debug (2/3)*0.20 + test 1*0.15, over weights 0.60.
    expected = 1 - (0.25 * 0.5 + 0.20 * (2 / 3) + 0.15 * 1.0) / 0.60
    assert n["norms_score"] == round(expected, 4)


def test_print_in_a_test_file_is_not_debris(tmp_path):
    fx, wd = _pair(
        tmp_path,
        {"m.py": "x = 1\n"},
        {"m.py": "x = 1\n", "m_test.py": "def test_x():\n    print('diag')\n"},
    )
    assert compute_norms(fx, wd, lint=False)["debug_leftovers"] == 0


def test_size_ratio_against_gold(tmp_path):
    fx, wd = _pair(
        tmp_path,
        {"c.yaml": "a: 1\n"},
        {"c.yaml": "a: 2\n" + "".join(f"k{i}: {i}\n" for i in range(9))},
    )
    n = compute_norms(fx, wd, gold_diff_lines=2, lint=False)
    # 11 diff lines vs a 2-line gold fix: ratio 5.5, 4.0 over the free 1.5x.
    assert n["diff_ratio"] == 5.5
    # Only debug (0) and size (saturated) apply: no code changed, no targets.
    assert n["norms_score"] == 0.5


def test_caches_and_hidden_dirs_are_ignored(tmp_path):
    fx, wd = _pair(
        tmp_path,
        {"m.py": "x = 1\n"},
        {
            "m.py": "x = 1\n",
            "__pycache__/m.cpython-312.pyc": "junk",
            ".pytest_cache/v/cache": "junk",
        },
    )
    n = compute_norms(fx, wd, lint=False)
    assert n["files_changed"] == 0 and n["norms_score"] == 1.0


def test_lint_delta_counts_only_new_findings(tmp_path, monkeypatch):
    fx, wd = _pair(
        tmp_path,
        {"m.py": "import os\n"},
        {"m.py": "import os\nimport sys\n"},
    )
    monkeypatch.setattr(norms, "_ruff_cmd", lambda: ["ruff"])
    monkeypatch.setattr(
        norms, "_ruff_count", lambda cmd, rel, text: text.count("import ")
    )
    assert compute_norms(fx, wd)["lint_delta"] == 1


def test_lint_is_skipped_when_ruff_is_unavailable(tmp_path, monkeypatch):
    fx, wd = _pair(tmp_path, {"m.py": "x = 1\n"}, {"m.py": "x = 2\n"})
    monkeypatch.setattr(norms, "_ruff_cmd", lambda: None)
    assert compute_norms(fx, wd)["lint_delta"] is None


def test_safe_norms_swallows_errors(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(norms, "compute_norms", boom)
    assert safe_norms(tmp_path, tmp_path, None) is None


def test_unified_diff_covers_edits_and_new_files(tmp_path):
    from bench.norms import unified_diff

    before, after = tmp_path / "a", tmp_path / "b"
    before.mkdir()
    after.mkdir()
    (before / "m.py").write_text("x = 1\n")
    (after / "m.py").write_text("x = 2\n")
    (after / "new.py").write_text("y = 1\n")
    diff = unified_diff(before, after)
    assert "-x = 1" in diff and "+x = 2" in diff
    assert "--- /dev/null" in diff and "+++ b/new.py" in diff
    assert unified_diff(before, after, cap=10).endswith("[diff truncated]")
