import json
import subprocess
import sys
from pathlib import Path


def test_unrelated_block_does_not_earn_check_credit(tmp_path):
    directory = Path(__file__).parent
    cases = json.loads((directory / "cases.json").read_text())
    results = {
        case["head"]: {
            "base": case["base"],
            "findings": [{"id": "matching"}, {"id": "unrelated"}],
            "matched_id": "matching",
            "blocking_ids": ["unrelated"],
            "conclusion": "failure",
        }
        for case in cases
        if case["split"] == "scored"
    }
    path = tmp_path / "results.json"
    path.write_text(json.dumps(results))
    output = subprocess.check_output(
        [sys.executable, directory / "score.py", path], text=True
    )
    assert "Agent finding recall: 100.0%" in output
    assert "Check conclusion recall: 0.0%" in output
