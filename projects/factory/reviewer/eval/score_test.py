import json
import shutil
import subprocess
import sys
from pathlib import Path


def test_unrelated_block_does_not_earn_check_credit(tmp_path):
    directory = Path(__file__).parent
    shutil.copy(directory / "score.py", tmp_path)
    cases = [
        {"head": "escape", "base": "base", "split": "scored", "answer": "defect"},
        {"head": "control", "base": "base", "split": "scored", "answer": None},
    ]
    (tmp_path / "cases.json").write_text(json.dumps(cases))
    results = {
        case["head"]: {
            "base": case["base"],
            "findings": [{"id": "matching"}, {"id": "unrelated"}],
            "matched_id": "matching" if case["answer"] else None,
            "blocking_ids": ["unrelated"],
            "conclusion": "failure",
        }
        for case in cases
        if case["split"] == "scored"
    }
    path = tmp_path / "results.json"
    path.write_text(json.dumps(results))
    output = subprocess.check_output(
        [sys.executable, tmp_path / "score.py", path], text=True
    )
    assert "Agent finding recall: 100.0%" in output
    assert "Check conclusion recall: 0.0%" in output
    assert "Control false-block rate: 100.0% (1/1)" in output


def test_existing_evidence_stops_before_replay(tmp_path):
    output = tmp_path / "results.json"
    output.write_text("prior evidence")
    result = subprocess.run(
        [sys.executable, Path(__file__).with_name("score.py"), "--reviewer", "absent"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "evidence destination already exists" in result.stderr
    assert output.read_text() == "prior evidence"
