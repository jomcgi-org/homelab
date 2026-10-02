import hashlib
import sys

import pytest

from bench.verifiers import get_verifier

MODULE = """def clamp(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x
"""

# Kills the lower-bound mutant only; the upper bound is never exercised.
LOWER_ONLY_TEST = """from pkg.mod import clamp

def test_lower():
    assert clamp(-1, 0, 10) == 0
    assert clamp(5, 0, 10) == 5
"""

SOURCE_TEXT_TEST = """from pathlib import Path

def test_source():
    assert "x < lo" in Path("pkg/mod.py").read_text()
"""

MUTANTS = [
    {"id": "lower", "find": "return lo", "replace": "return x"},
    {"id": "upper", "find": "return hi", "replace": "return x"},
]


def _args(**over):
    args = {
        "python": sys.executable,
        "module": "pkg/mod.py",
        "module_sha256": hashlib.sha256(MODULE.encode()).hexdigest(),
        "tests": ["pkg/mod_test.py"],
        "mutants": MUTANTS,
        "equivalent": [
            {"id": "eq-swap", "find": "if x < lo:", "replace": "if lo > x:"}
        ],
        "pass_threshold": 0.5,
    }
    args.update(over)
    return args


def _workdir(tmp_path, test_src, module=MODULE):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(module)
    (tmp_path / "pkg" / "mod_test.py").write_text(test_src)
    return tmp_path


def test_score_is_fraction_of_mutants_killed(tmp_path):
    r = get_verifier("mutation")(_workdir(tmp_path, LOWER_ONLY_TEST), _args())
    assert r.score == 0.5
    assert r.passed
    assert "killed 1/2" in r.feedback and "survived: upper" in r.feedback


def test_threshold_gates_passed(tmp_path):
    r = get_verifier("mutation")(
        _workdir(tmp_path, LOWER_ONLY_TEST), _args(pass_threshold=1.0)
    )
    assert r.score == 0.5 and not r.passed


def test_killing_an_equivalent_mutant_zeroes_the_score(tmp_path):
    r = get_verifier("mutation")(_workdir(tmp_path, SOURCE_TEXT_TEST), _args())
    assert r.score == 0.0 and not r.passed
    assert "eq-swap" in r.feedback


def test_modified_module_scores_zero(tmp_path):
    wd = _workdir(tmp_path, LOWER_ONLY_TEST, module=MODULE + "# edited\n")
    r = get_verifier("mutation")(wd, _args())
    assert r.score == 0.0 and "was modified" in r.feedback


def test_suite_failing_on_original_scores_zero(tmp_path):
    wd = _workdir(tmp_path, "def test_bad():\n    assert False\n")
    r = get_verifier("mutation")(wd, _args())
    assert r.score == 0.0 and "unmodified module" in r.feedback


def test_missing_test_file_scores_zero(tmp_path):
    wd = _workdir(tmp_path, LOWER_ONLY_TEST)
    r = get_verifier("mutation")(wd, _args(tests=["pkg/other_test.py"]))
    assert r.score == 0.0 and "no test file" in r.feedback


def test_stale_mutant_fails_loudly(tmp_path):
    wd = _workdir(tmp_path, LOWER_ONLY_TEST)
    stale = [{"id": "gone", "find": "not in the module", "replace": "x"}]
    with pytest.raises(ValueError, match="gone"):
        get_verifier("mutation")(wd, _args(mutants=stale))
