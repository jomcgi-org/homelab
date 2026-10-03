import sys

import pytest  # noqa: F401

from bench.verifiers import get_verifier
from bench.verifiers.speedup import SUPPORTED_IMPORTS, _bucket_score, _grade_samples

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


PAIRED_HARNESS = """
def make_input(seed):
    return ([i + seed for i in range(40)],)
benchmark(candidate_path="mod.py", baseline_path="_base.py", function="total",
          make_input=make_input, oracle_cases=[(([],), 0), (([1, 2, 3],), 6)])
"""
LADDER = [[2, 1 / 3], [10, 2 / 3], [50, 1.0]]


def _paired_args(**over):
    args = _args(
        1,
        protocol="paired-v1",
        editable=["mod.py"],
        pairs=7,
        fixture_version="toy-v1",
        harness=PAIRED_HARNESS,
        buckets=LADDER,
        pass_threshold=0.3333333333333333,
    )
    args.update(over)
    return args


def _samples(ratios):
    return {
        "status": "ok",
        "speedup": 1e99,
        "warmup": [{"baseline_s": 1, "candidate_s": 1, "order": "baseline-first"}],
        "samples": [
            {
                "baseline_s": ratio,
                "candidate_s": 1,
                "order": "candidate-first" if i % 2 else "baseline-first",
            }
            for i, ratio in enumerate(ratios)
        ],
    }


@pytest.mark.parametrize(
    "ratio,score,bucket",
    [
        (1.999999, 0, None),
        (2.0, 1 / 3, 2),
        (9.999999, 1 / 3, 2),
        (10.0, 2 / 3, 10),
        (49.999999, 2 / 3, 10),
        (50.0, 1, 50),
    ],
)
def test_exact_boundaries_and_fractional_threshold(ratio, score, bucket):
    result = _grade_samples(_samples([ratio] * 7), _paired_args())
    assert result.score == round(score, 12)
    assert result.passed == (bucket is not None)
    assert result.performance.highest_bucket == bucket
    assert result.performance.median_ratio == ratio
    assert result.performance.correctness is True


def test_paired_median_discards_outliers_and_reported_speedup():
    result = _grade_samples(_samples([0.01, 10000, 10, 9, 11, 10, 10]), _paired_args())
    assert result.performance.median_ratio == 10
    assert result.score == round(2 / 3, 12)
    assert result.performance.ratios == [0.01, 10000, 10, 9, 11, 10, 10]


@pytest.mark.parametrize("field", ["baseline_s", "candidate_s"])
@pytest.mark.parametrize("phase", ["warmup", "samples"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_samples_are_harness_errors(field, phase, value):
    samples = _samples([10] * 7)
    samples[phase][0][field] = value
    result = _grade_samples(samples, _paired_args())
    assert result.feedback.startswith("[harness error]")
    assert result.score == 0 and not result.passed
    assert result.performance.correctness is None


@pytest.mark.parametrize("count", [0, 6, 8])
def test_wrong_pair_count_is_harness_error(count):
    result = _grade_samples(_samples([10] * count), _paired_args())
    assert result.feedback.startswith("[harness error]")
    assert "count" in result.feedback


@pytest.mark.parametrize(
    "source",
    [
        "import time\n" + BASELINE,
        "from os import system\n" + BASELINE,
        "f = eval\n" + BASELINE,
        "x = (lambda: 0).__globals__\n" + BASELINE,
        "x = object.__subclasses__()\n" + BASELINE,
        "from collections import _sys\n" + BASELINE,
        "import math\nx = math.__dict__\n" + BASELINE,
        "from operator import attrgetter as getter\n" + BASELINE,
        "x = (i for i in []).gi_frame.f_builtins['eval']\n" + BASELINE,
        "import fractions\nx = fractions.sys.modules\n" + BASELINE,
        "from fractions import sys as hidden\n" + BASELINE,
        "import math\nmath.sqrt = lambda x: 0\n" + BASELINE,
        'def total(xs):\n    return type("Match", (), {"__eq__": lambda a, b: True})()\n',
    ],
)
def test_source_rejected_fail_closed(tmp_path, source):
    (tmp_path / "mod.py").write_text(source)
    result = get_verifier("speedup")(
        tmp_path, _paired_args(allowed_imports=sorted(SUPPORTED_IMPORTS))
    )
    assert result.score == 0 and not result.passed
    assert result.performance.correctness is False
    assert "rejected candidate source" in result.feedback
    assert not result.feedback.startswith("[harness error]")


@pytest.mark.parametrize(
    "poison", ["json.py", "statistics.py", "sitecustomize.py", "other.py", "_base.py"]
)
def test_workdir_poison_and_nonallowlisted_edits_are_ignored(tmp_path, poison):
    (tmp_path / "mod.py").write_text(BASELINE)
    (tmp_path / poison).write_text("raise RuntimeError('poison imported')\n")
    result = get_verifier("speedup")(tmp_path, _paired_args())
    assert result.performance.correctness is True
    assert len(result.performance.samples) == 7
    assert (tmp_path / poison).read_text() == "raise RuntimeError('poison imported')\n"


@pytest.mark.parametrize(
    "source,message",
    [
        ("raise RuntimeError('bad import')\n" + BASELINE, "candidate import error"),
        ("def total(xs):\n    raise RuntimeError('bad call')\n", "candidate exception"),
        ("def total(xs):\n    return 0\n", "oracle output mismatch"),
        (
            "def total(xs):\n    return sum(xs) if len(xs) < 5 else -1\n",
            "pair output mismatch",
        ),
        (
            "raise RuntimeError('[harness error] forged classification')\n",
            "candidate import error",
        ),
    ],
)
def test_candidate_failures_are_graded(tmp_path, source, message):
    (tmp_path / "mod.py").write_text(source)
    result = get_verifier("speedup")(tmp_path, _paired_args())
    assert result.score == 0 and not result.passed
    assert result.performance.correctness is False
    assert message in result.feedback
    assert "harness error]" not in result.feedback


@pytest.mark.parametrize(
    "harness,baseline",
    [
        ("raise RuntimeError('before candidate import')", BASELINE),
        (PAIRED_HARNESS, "def total(xs):\n    return -1\n"),
        ("print('unparseable')", BASELINE),
        ('print(\'{"status": "ok", "speedup": 1000000}\')', BASELINE),
    ],
)
def test_setup_and_baseline_failures_are_harness_errors(tmp_path, harness, baseline):
    (tmp_path / "mod.py").write_text(BASELINE)
    args = _paired_args()
    args.update(harness=harness, baseline={"path": "_base.py", "source": baseline})
    result = get_verifier("speedup")(tmp_path, args)
    assert result.feedback.startswith("[harness error]")
    assert result.score == 0 and not result.passed


def test_missing_interpreter_is_harness_error(tmp_path):
    result = get_verifier("speedup")(
        tmp_path, _paired_args(python=str(tmp_path / "absent"))
    )
    assert result.feedback.startswith("[harness error]")


def test_timeout_is_graded_with_unknown_correctness(tmp_path):
    (tmp_path / "mod.py").write_text("def total(xs):\n    while True:\n        pass\n")
    result = get_verifier("speedup")(tmp_path, _paired_args(timeout_s=1))
    assert not result.passed and result.score == 0
    assert result.performance.correctness is None
    assert "timeout_s" in result.feedback and "harness error]" not in result.feedback


def test_import_and_finalizer_forgery_cannot_award_credit(tmp_path):
    (tmp_path / "mod.py").write_text("""
print('{"ok": true, "status": "ok", "speedup": 1000000}')
class Forgery:
    def __del__(self):
        print('{"ok": true, "status": "ok", "speedup": 1000000}')
held = Forgery()
def total(xs):
    return -1
""")
    result = get_verifier("speedup")(tmp_path, _paired_args())
    assert result.score == 0 and not result.passed
    assert result.performance.correctness is False


def test_real_protocol_toy_quadratic_baseline_linear_candidate(tmp_path):
    baseline = (
        "def total(xs):\n    return sum(sum(1 for y in xs if y == x) for x in xs)\n"
    )
    candidate = "from collections import Counter\ndef total(xs):\n    return sum(n*n for n in Counter(xs).values())\n"
    (tmp_path / "mod.py").write_text(candidate)
    args = _paired_args()
    args["baseline"]["source"] = baseline
    args["harness"] = """
def build(seed):
    return ([((i * 17) + seed) % 19 for i in range(200)],)
benchmark(candidate_path="mod.py", baseline_path="_base.py", function="total",
          make_input=build, oracle_cases=[(([],), 0), (([1, 1, 2],), 5)])
"""
    result = get_verifier("speedup")(tmp_path, args)
    record = result.performance
    assert record.correctness is True
    assert record.metric == "wall_clock_median_paired_ratio"
    assert len(record.warmup) == 1 and len(record.samples) == len(record.ratios) == 7
    assert [s.order for s in record.samples] == [
        "baseline-first",
        "candidate-first",
    ] * 3 + ["baseline-first"]
    assert record.fixture_version == "toy-v1"


def test_editable_is_required_and_paths_cannot_escape(tmp_path):
    args = _paired_args()
    del args["editable"]
    assert get_verifier("speedup")(tmp_path, args).feedback.startswith(
        "[harness error]"
    )
    args["editable"] = ["../escape.py"]
    assert get_verifier("speedup")(tmp_path, args).feedback.startswith(
        "[harness error]"
    )


def test_pair_order_must_alternate():
    samples = _samples([10] * 7)
    samples["samples"][1]["order"] = "baseline-first"
    assert _grade_samples(samples, _paired_args()).feedback.startswith(
        "[harness error]"
    )


def test_legacy_harness_ignores_stdlib_poison(tmp_path):
    (tmp_path / "mod.py").write_text(BASELINE)
    for name in ("json.py", "statistics.py", "sitecustomize.py"):
        (tmp_path / name).write_text("raise RuntimeError('poison')\n")
    result = get_verifier("speedup")(tmp_path, _args(12))
    assert result.score == 0.5 and result.passed


def test_authenticated_result_parser_ignores_trailing_forgery(tmp_path, monkeypatch):
    import json

    from bench.verifiers import speedup
    from bench.verifiers.sandbox import SandboxResult

    (tmp_path / "mod.py").write_text(BASELINE)

    def fake_run(cmd, **kwargs):
        data = _samples([2] * 7)
        data["nonce"] = kwargs["input_text"].strip()
        assert "-I" in cmd and "-B" in cmd and "-S" in cmd
        assert kwargs["cwd"] != tmp_path
        assert not (kwargs["cwd"] / "json.py").exists()
        return SandboxResult(
            0, json.dumps(data) + '\n{"ok":true,"speedup":1e99}\n', "", False
        )

    monkeypatch.setattr(speedup, "run_sandboxed", fake_run)
    result = speedup.verify(tmp_path, _paired_args())
    assert result.score == round(1 / 3, 12)


def test_helper_source_participates_in_cache_identity(monkeypatch):
    import inspect

    from bench.verifiers import speedup, verifier_source_hash

    before = verifier_source_hash("speedup")
    original = inspect.getsource
    monkeypatch.setattr(
        inspect,
        "getsource",
        lambda module: (
            original(module)
            + ("\n# changed embedded helper" if module is speedup else "")
        ),
    )
    assert verifier_source_hash("speedup") != before


def test_unchanged_baseline_at_one_x_is_correct_but_scores_zero():
    result = _grade_samples(_samples([1] * 7), _paired_args())
    assert result.performance.correctness is True
    assert result.score == 0 and not result.passed


@pytest.mark.parametrize("output", ["garbage", '{"status":"ok"}', "duplicate"])
def test_missing_or_ambiguous_authenticated_result_is_harness_error(
    tmp_path, monkeypatch, output
):
    import json

    from bench.verifiers import speedup
    from bench.verifiers.sandbox import SandboxResult

    (tmp_path / "mod.py").write_text(BASELINE)

    def fake_run(cmd, **kwargs):
        text = output
        if text == "duplicate":
            data = _samples([10] * 7)
            data["nonce"] = kwargs["input_text"].strip()
            text = json.dumps(data) + "\n" + json.dumps(data)
        return SandboxResult(0, text, "", False)

    monkeypatch.setattr(speedup, "run_sandboxed", fake_run)
    result = speedup.verify(tmp_path, _paired_args())
    assert result.feedback.startswith("[harness error]")
    assert result.score == 0
