import json
import subprocess
from types import SimpleNamespace

import pytest

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
    penalties = {
        "scope": 0.5,
        "debug": 2 / 3,
        "test": 1.0,
        "comments": (1 / 4 - 0.10) / 0.30,
    }
    expected = 1 - sum(norms.WEIGHTS[k] * p for k, p in penalties.items()) / sum(
        norms.WEIGHTS[k] for k in penalties
    )
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
    monkeypatch.setattr(norms, "_check_version", lambda *a: None)
    from collections import Counter

    monkeypatch.setattr(
        norms,
        "_ruff_findings",
        lambda cmd, rel, text: Counter(
            {(rel, "F401", "unused"): text.count("import ")}
        ),
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


@pytest.mark.parametrize(
    "suffix,text,expected",
    [
        (
            ".py",
            'x = "# not a comment"\n"""docstring # also not a comment"""\n# comment\nx = 2 # inline\n',
            {2, 3},
        ),
        (".py", '"""multi\n# docstring\n"""\n', set()),
        (
            ".go",
            'var x = "// literal"\nvar y = `/* literal */`\n/* block\n * body\n */\n// line\n',
            {2, 3, 4, 5},
        ),
        (
            ".js",
            "const x = \"// literal\";\nconst y = '/* literal */';\nconst z = `// literal`;\n/* block */\n",
            {3},
        ),
        (".ts", "const x = `escaped \\` // literal`;\n// real\n", {1}),
        (".js", "const x = `outer ${`/* literal */`}`;\n", set()),
        (".ts", "const x = `outer ${`/* literal */`}`;\n", set()),
        (".js", "const x = `outer ${ /* real comment */ 1}`;\n", {0}),
        (".ts", "const x = `outer ${ /* real comment */ 1}`;\n", {0}),
        (
            ".js",
            "const x = `outer ${`inner ${1 /* deep */}`}`;\n",
            {0},
        ),
        (".ts", "const x = `a ${ {b: 1} /* c */ }`;\n", {0}),
        (".js", "const re = /x/;\n", set()),
        (".ts", "const re = /[//]/;\n", set()),
        (".js", "const re = /[//]/;\n", set()),
        (".ts", "const re = /[/*]/;\n", set()),
        (".js", "const re = /[/*]/;\n", set()),
        (".js", "const re = /a\\/\\/b/g; // real\n", {0}),
        (".ts", "const re = /['\"`]/; // real\n", {0}),
        (".js", "const m = x.match(/\\/*/);\nreturn /[/*]/.test(s);\n", set()),
        (".ts", "const a = b / c; // d / e\nconst f = g[0] / 2 /* h */;\n", {0, 1}),
        (".js", "function f() { return typeof /[/*]/; }\n", set()),
        (".ts", "function f() { return typeof /[/*]/; }\n", set()),
        (".js", "const r = x\n  ? /[//]/ : /a/; // real\n", {1}),
        (".js", "const q = a / 2; const s = '/*';\n", set()),
        (".js", "const value = obj.return / 2; // real comment\n", {0}),
        (".ts", "const value = obj.return / 2; // real comment\n", {0}),
        (".js", "const value = obj.return / /[//]/.source.length;\n", set()),
        (".ts", "const value = obj.return / /[//]/.source.length;\n", set()),
        (".js", "const value = obj?.typeof / 2; // real\n", {0}),
        (".ts", "const value = 1. / /[//]/.source.length;\n", set()),
        (".js", "const value = 1. / /[//]/.source.length;\n", set()),
        (".js", "const value = 1.5 / 2; // real\n", {0}),
        (".ts", "const value = 1..toFixed / 2; // real\n", {0}),
        (".ts", "const x = `${/[//]/.test(y)} // literal`;\n", set()),
    ],
)
def test_comment_scanner_ignores_literals(suffix, text, expected):
    assert norms._comment_lines(text, suffix) == expected


@pytest.mark.parametrize("suffix", [".js", ".ts"])
@pytest.mark.parametrize(
    "text",
    [
        "if (ok) /[//]/.test(value);\n",
        "if (ok) /[/*]/.test(value);\n",
        "const value = n++ / 2; // real comment\n",
        "const value = (a + b) / 2;\n",
        "const value = n-- / 2;\n",
        "if (ok) {}\n/x/.test(value);\n",
        "const value = a / 2 / b / /unterminated;\n",
    ],
)
def test_comment_scanner_rejects_ambiguous_slash(suffix, text):
    with pytest.raises(ValueError):
        norms._comment_lines(text, suffix)


@pytest.mark.parametrize("rel", ["a.js", "a.ts"])
@pytest.mark.parametrize(
    "text",
    [
        "if (ok) /[//]/.test(value);\n",
        "if (ok) /[/*]/.test(value);\n",
        "const value = n++ / 2; // real comment\n",
    ],
)
def test_comment_density_is_unmeasured_for_ambiguous_slash(tmp_path, rel, text):
    fx, wd = _pair(tmp_path, {rel: "let y = 1;\n"}, {rel: "let y = 1;\n" + text})
    n = compute_norms(fx, wd, lint=False)
    assert n["comment_density_added"] is None
    assert n["comment_density_baseline"] is None
    assert n["comment_density_delta"] is None


def test_comment_density_pools_added_lines_and_baselines(tmp_path):
    fx, wd = _pair(
        tmp_path,
        {"a.py": "# old\nx = 1\n", "b.ts": "let y = 1;\n"},
        {
            "a.py": "# old\nx = 2\n# new\n",
            "b.ts": "let y = 2;\n// new\n",
            "test_m.py": "# ignored\n",
        },
    )
    n = compute_norms(fx, wd, lint=False)
    assert n["comment_density_added"] == 0.5
    assert n["comment_density_baseline"] == 1 / 3
    assert n["comment_density_delta"] == 0.5 - 1 / 3
    assert n["norms_version"] == 2
    assert n == compute_norms(fx, wd, lint=False)
    assert sum(norms.WEIGHTS.values()) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "rel,old,new",
    [
        ("a.rs", "let x = 1;\n", "// comment\nlet x = 2;\n"),
        ("a.py", "x = 1\n", ""),
        ("a.py", "x = 1\n", "x = 1\n\n"),
        ("a.py", "x = 1\n", 'x = """unterminated\n'),
        ("a.go", "package a\n", "package a\n/* unterminated\n"),
        ("a_test.go", "package a\n", "package a\n// test\n"),
    ],
)
def test_comment_density_unmeasured_is_none(tmp_path, rel, old, new):
    fx, wd = _pair(tmp_path, {rel: old}, {rel: new})
    n = compute_norms(fx, wd, lint=False)
    assert n["comment_density_added"] is None
    assert n["comment_density_baseline"] is None
    assert n["comment_density_delta"] is None


@pytest.mark.parametrize("density,penalty", [(0.1, 0), (0.25, 0.5), (0.4, 1), (1, 1)])
def test_comment_penalty_boundaries(tmp_path, density, penalty):
    count = int(density * 100)
    new = "# comment\n" * count + "x = 2\n" * (100 - count)
    fx, wd = _pair(tmp_path, {"m.py": "x = 1\n"}, {"m.py": new})
    n = compute_norms(fx, wd, lint=False)
    expected = 1 - (norms.WEIGHTS["test"] + norms.WEIGHTS["comments"] * penalty) / sum(
        norms.WEIGHTS[k] for k in ("debug", "test", "comments")
    )
    assert n["norms_score"] == round(expected, 4)


def _lint_pair(tmp_path, language):
    suffix = ".go" if language == "go" else ".py"
    module = {"go.mod": "module example.test/m\ngo 1.23\n"} if language == "go" else {}
    return _pair(
        tmp_path, {**module, "m" + suffix: "old\n"}, {**module, "m" + suffix: "new\n"}
    )


def _mock_linter(monkeypatch, language, baseline, current, *, error=None):
    monkeypatch.setattr(norms.shutil, "which", lambda name: name)

    def run(cmd, **kwargs):
        if "--version" in cmd:
            version = (
                norms.GOLANGCI_LINT_VERSION if language == "go" else norms.RUFF_VERSION
            )
            return SimpleNamespace(returncode=0, stdout=f"version {version}", stderr="")
        if error:
            raise error
        if language == "go":
            assert "--show-stats=false" in cmd
            env = kwargs["env"]
            assert env["GOPROXY"] == "off" and env["GONOPROXY"] == "none"
            assert env["GOFLAGS"] == "-mod=readonly" and env["GOTOOLCHAIN"] == "local"
            text = (kwargs["cwd"] / "m.go").read_text()
            records = current if text == "new\n" else baseline
            payload = {
                "Issues": [
                    {
                        "FromLinter": code,
                        "Text": msg,
                        "Pos": {"Filename": "m.go", "Line": line},
                    }
                    for code, msg, line in records
                ]
            }
            # A tool may write caches; those writes must remain on the lint copy.
            (kwargs["cwd"] / "linter-artifact").write_text("cache")
        else:
            records = current if kwargs["input"] == "new\n" else baseline
            payload = [
                {"code": code, "message": msg, "location": {"row": line}}
                for code, msg, line in records
            ]
        return SimpleNamespace(
            returncode=1 if records else 0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(norms.subprocess, "run", run)


@pytest.mark.parametrize("language", ["go", "python"])
@pytest.mark.parametrize(
    "baseline,current,expected",
    [
        ([("W1", "old warning", 1)], [("W1", "old warning", 40)], 0),
        ([("W1", "old warning", 1)], [("W2", "new warning", 1)], 1),
        ([("W1", "duplicate", 1)], [("W1", "duplicate", 1), ("W1", "duplicate", 2)], 1),
    ],
)
def test_lint_compares_diagnostic_multisets(
    tmp_path, monkeypatch, language, baseline, current, expected
):
    fx, wd = _lint_pair(tmp_path, language)
    _mock_linter(monkeypatch, language, baseline, current)
    n = compute_norms(fx, wd)
    assert n["lint_delta"] == expected
    assert n["lint_unavailable"] is None
    assert (
        not (fx / "linter-artifact").exists() and not (wd / "linter-artifact").exists()
    )


@pytest.mark.parametrize("language", ["go", "python"])
def test_lint_timeout_is_unavailable(tmp_path, monkeypatch, language):
    fx, wd = _lint_pair(tmp_path, language)
    _mock_linter(
        monkeypatch, language, [], [], error=subprocess.TimeoutExpired("lint", 60)
    )
    n = compute_norms(fx, wd)
    assert n["lint_delta"] is None
    assert "timed out" in n["lint_unavailable"]


def test_go_only_without_linter_is_unavailable(tmp_path, monkeypatch):
    fx, wd = _lint_pair(tmp_path, "go")
    monkeypatch.setattr(norms.shutil, "which", lambda name: None)
    n = compute_norms(fx, wd)
    assert n["lint_delta"] is None
    assert n["lint_unavailable"] == "golangci-lint not on PATH"


@pytest.mark.parametrize("language", ["go", "python"])
@pytest.mark.parametrize("failure", ["version", "exit", "json", "empty-findings"])
def test_lint_failures_are_unavailable(tmp_path, monkeypatch, language, failure):
    fx, wd = _lint_pair(tmp_path, language)
    monkeypatch.setattr(norms.shutil, "which", lambda name: name)

    def run(cmd, **kwargs):
        if "--version" in cmd:
            version = (
                norms.GOLANGCI_LINT_VERSION if language == "go" else norms.RUFF_VERSION
            )
            return SimpleNamespace(
                returncode=0,
                stdout="0.0.0" if failure == "version" else version,
                stderr="",
            )
        stdout = "broken-json"
        if failure == "empty-findings":
            stdout = json.dumps({"Issues": []} if language == "go" else [])
        return SimpleNamespace(
            returncode=2
            if failure == "exit"
            else 1
            if failure == "empty-findings"
            else 0,
            stdout=stdout,
            stderr="analysis failed",
        )

    monkeypatch.setattr(norms.subprocess, "run", run)
    n = compute_norms(fx, wd)
    assert n["lint_delta"] is None and n["lint_unavailable"]


def test_go_missing_module_is_unavailable(tmp_path, monkeypatch):
    fx, wd = _pair(tmp_path, {"m.go": "old\n"}, {"m.go": "new\n"})
    _mock_linter(monkeypatch, "go", [], [])
    n = compute_norms(fx, wd)
    assert n["lint_delta"] is None and "missing go.mod" in n["lint_unavailable"]


def test_non_lintable_change_is_measured_zero(tmp_path, monkeypatch):
    fx, wd = _pair(tmp_path, {"a.yaml": "x: 1\n"}, {"a.yaml": "x: 2\n"})
    monkeypatch.setattr(norms.shutil, "which", lambda name: None)
    assert compute_norms(fx, wd)["lint_delta"] == 0


def test_gold_diff_projects_snapshot_from_synthetic_history(tmp_path):
    from bench.norms import gold_diff_size, project_snapshot_path

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init")
    _tree(
        repo,
        {
            "project/pkg/a.py": "x = 1\n",
            "project/pkg/a_test.py": "old test\n",
            "project/pkg/skip/b.py": "old\n",
            "other/a.py": "old\n",
        },
    )
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "test: baseline",
    )
    parent = git("rev-parse", "HEAD")
    _tree(
        repo,
        {
            "project/pkg/a.py": "x = 2\ny = 3\n",
            "project/pkg/a_test.py": "new test\n",
            "project/pkg/skip/b.py": "new\n",
            "other/a.py": "new\n",
        },
    )
    (repo / "project/pkg/binary.bin").write_bytes(b"\0binary")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "test: fix",
    )
    source = git("rev-parse", "HEAD")
    snap = {
        "commit": parent,
        "paths": ["project/pkg"],
        "strip_components": 2,
        "exclude": ["*_test.py", "skip/"],
    }
    assert project_snapshot_path("other/a.py", snap) is None
    assert project_snapshot_path("project/pkg/skip/b.py", snap) is None
    assert project_snapshot_path("project/pkg/a.py", snap) == "a.py"
    assert gold_diff_size(repo, source, snap) == (3, None)
    assert gold_diff_size(
        repo,
        source,
        {**snap, "overlays": [{"commit": source, "paths": ["other/a.py"]}]},
    ) == (3, None)
    size, reason = gold_diff_size(
        repo,
        source,
        {**snap, "overlays": [{"commit": source, "paths": ["project/pkg/a.py"]}]},
    )
    assert size is None and "overlay" in reason
    size, reason = gold_diff_size(repo, source, {**snap, "commit": source})
    assert size is None and "pre-fix" in reason
    assert (
        gold_diff_size(repo, source, {**snap, "patches": [{"file": "a.py"}]})[0] is None
    )
