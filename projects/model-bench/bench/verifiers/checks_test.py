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


RESPONSE_SCRIPT = """import json, os
from pathlib import Path

captured = Path(os.environ["RESPONSE_FILE"])
written = Path("response.txt").read_text() if Path("response.txt").exists() else ""
print(json.dumps({"checks": {
    "captured": captured.read_text() == "the model said this",
    "file_ignored": written != captured.read_text(),
    "outside_workdir": not captured.resolve().is_relative_to(Path.cwd().resolve()),
}}))
"""


def test_captured_response_reaches_the_script_outside_the_workdir(tmp_path):
    # A file the model wrote under the same name is not the response: the harness
    # copy lives outside the workdir and carries what the model said.
    (tmp_path / "response.txt").write_text("the model wrote this")
    r = get_verifier("checks")(
        tmp_path, _args(script=RESPONSE_SCRIPT, response="the model said this")
    )
    assert r.score == 1.0, r.feedback


def test_missing_response_is_an_empty_file(tmp_path):
    script = (
        "import json, os\nfrom pathlib import Path\n"
        'print(json.dumps({"checks": {'
        '"empty": Path(os.environ["RESPONSE_FILE"]).read_text() == ""}}))'
    )
    r = get_verifier("checks")(tmp_path, _args(script=script))
    assert r.score == 1.0, r.feedback
