import sys

import pytest

from bench.verifiers import get_verifier

SCRIPT = """import json
from pathlib import Path

text = Path("answer.txt").read_text() if Path("answer.txt").exists() else ""
print("debug noise")
print(json.dumps({"checks": {"has_a": "a" in text, "has_b": "b" in text}}))
"""


def _args(**over):
    args = {"python": sys.executable, "script": SCRIPT, "pass_threshold": 1.0}
    args.update(over)
    return args


def test_all_checks_pass(tmp_path):
    (tmp_path / "answer.txt").write_text("ab")
    r = get_verifier("checks")(tmp_path, _args())
    assert r.passed and r.score == 1.0 and r.feedback == "2/2 checks passed"


def test_partial_credit_is_the_weighted_mean(tmp_path):
    (tmp_path / "answer.txt").write_text("a")
    r = get_verifier("checks")(tmp_path, _args(weights={"has_a": 3, "has_b": 1}))
    assert r.score == 0.75 and not r.passed
    assert "failed: has_b" in r.feedback


def test_threshold_gates_passed(tmp_path):
    (tmp_path / "answer.txt").write_text("a")
    r = get_verifier("checks")(tmp_path, _args(pass_threshold=0.5))
    assert r.passed and r.score == 0.5


def test_unreported_weighted_check_counts_as_failed(tmp_path):
    (tmp_path / "answer.txt").write_text("ab")
    r = get_verifier("checks")(tmp_path, _args(weights={"has_a": 1, "has_c": 1}))
    # has_b is unweighted (1), has_c is missing: 2 of 3 weight units.
    assert r.score == pytest.approx(2 / 3)
    assert "has_c" in r.feedback


def test_crashing_script_scores_zero(tmp_path):
    r = get_verifier("checks")(tmp_path, _args(script="raise SystemExit('boom')"))
    assert r.score == 0.0 and not r.passed and "boom" in r.feedback


def test_helm_is_passed_to_the_script(tmp_path):
    script = 'import json, os\nprint(json.dumps({"checks": {"helm": os.environ["HELM"] == "/x/helm"}}))'
    r = get_verifier("checks")(tmp_path, _args(script=script, helm="/x/helm"))
    assert r.score == 1.0
