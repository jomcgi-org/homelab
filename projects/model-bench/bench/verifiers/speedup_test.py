import sys

import pytest  # noqa: F401

from bench.verifiers import get_verifier
from bench.verifiers.speedup import _bucket_score

BASELINE = "def total(xs):\n    return sum(xs)\n"

# A stand-in harness: reports a fixed speedup only if the candidate agrees with
# the baseline, mirroring the real contract (correctness first, then timing).
HARNESS = """import json, sys
sys.path.insert(0, ".")
import _base, mod
ok = mod.total([1, 2, 3]) == _base.total([1, 2, 3])
print("noise line")
print(json.dumps({"ok": ok, "speedup": float(sys.argv[1]) if ok else 0.0,
                  "detail": "" if ok else "output differs"}))
"""

BUCKETS = [[3, 0.25], [10, 0.5], [30, 1.0]]


def _args(speedup, **over):
    args = {
        "python": sys.executable,
        "baseline": {"path": "_base.py", "source": BASELINE},
        "harness": HARNESS,
        "harness_args": [speedup],
        "buckets": BUCKETS,
        "pass_threshold": 0.5,
    }
    args.update(over)
    return args


def test_bucket_score_picks_highest_cleared_bucket():
    assert _bucket_score(1.9, BUCKETS) == 0.0
    assert _bucket_score(3.0, BUCKETS) == 0.25
    assert _bucket_score(29.9, BUCKETS) == 0.5
    assert _bucket_score(500, BUCKETS) == 1.0


def test_speedup_scores_by_bucket(tmp_path):
    (tmp_path / "mod.py").write_text(BASELINE)
    r = get_verifier("speedup")(tmp_path, _args(12.0))
    assert r.score == 0.5 and r.passed
    assert "12.0x" in r.feedback


def test_wrong_output_scores_zero_however_fast(tmp_path):
    (tmp_path / "mod.py").write_text("def total(xs):\n    return 0\n")
    r = get_verifier("speedup")(tmp_path, _args(100.0))
    assert r.score == 0.0 and not r.passed
    assert "differs" in r.feedback


def test_crashing_harness_scores_zero(tmp_path):
    # No candidate module: the harness import fails before printing a result.
    r = get_verifier("speedup")(tmp_path, _args(100.0))
    assert r.score == 0.0 and "no result" in r.feedback
