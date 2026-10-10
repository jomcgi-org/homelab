"""Real campsites task-pack grading and adversarial probes, with no model API."""

import argparse
import copy
import json
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from bench.cli import _snapshot, _verifier_cache_repr
from bench.schema import TaskSpec
from bench.verifiers.speedup import _bucket_score, verify

TASK_DIR = Path(__file__).resolve().parents[1] / "tasks/campsites-region-rollup-perf-01"


@pytest.fixture
def task():
    # Missing Bazel runfiles must fail, never silently skip grading coverage.
    return yaml.safe_load((TASK_DIR / "task.yaml").read_text())


@pytest.fixture
def fixture(tmp_path, task):
    task_dir = tmp_path / "tasks" / task["id"]
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text((TASK_DIR / "task.yaml").read_text())
    _snapshot(argparse.Namespace(tasks=task_dir.parent, task=task["id"], repo=tmp_path))
    return task_dir / "fixture"


def small_args(task):
    args = copy.deepcopy(task["verifier"]["args"])
    # Keep all one-warmup/seven-pair measurements, shrink only the dataset.
    args["harness_args"] = [70]
    args["python"] = sys.executable
    return args


def reference(name):
    return (TASK_DIR / "reference" / (name + ".py")).read_text()


def harness_globals(task):
    recorded = {}

    def capture(**kwargs):
        recorded.update(kwargs)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "harness.py"
        path.write_text(task["verifier"]["args"]["harness"])
        namespace = runpy.run_path(str(path), init_globals={"benchmark": capture})
    return namespace, recorded


def test_seeded_source_and_task_contract(task, fixture):
    spec = TaskSpec.model_validate(task)
    args = task["verifier"]["args"]
    assert spec.agent.exec and spec.agent.max_turns == 40
    assert (spec.mode, spec.tier, spec.task_class.value) == (
        "agentic",
        "hard",
        "code-fix",
    )
    assert spec.target_files == args["editable"] == ["rollup.py"]
    assert set(task["snapshot"]["files"]) == {"rollup.py", "test_rollup.py"}
    assert (fixture / "rollup.py").read_bytes() == args["baseline"]["source"].encode()
    assert args["protocol"] == "paired-v1" and args["pairs"] == 7
    assert args["harness_args"] == [180] and args["timeout_s"] == 120
    assert args["fixture_version"] == "campsites-rollup-seeded-v1"
    assert spec.source_commit in args["baseline"]["source"]
    assert round(args["pass_threshold"], 12) == _bucket_score(2, args["buckets"])
    assert [_bucket_score(ratio, args["buckets"]) for ratio in (1.99, 2, 10, 50)] == [
        0,
        round(1 / 3, 12),
        round(2 / 3, 12),
        1,
    ]
    assert set(args["allowed_imports"]) == {
        "__future__",
        "collections",
        "itertools",
        "functools",
        "operator",
        "math",
        "bisect",
        "heapq",
        "datetime",
    }
    assert not any(path.name == "reference" for path in fixture.rglob("*"))


@pytest.mark.parametrize("candidate", ["baseline", "micro", "algorithmic"])
def test_visible_tests_run_on_each_correct_implementation(task, fixture, candidate):
    if candidate != "baseline":
        (fixture / "rollup.py").write_text(reference(candidate))
    result = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "-v", "test_rollup"],
        cwd=fixture,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stderr


def test_protected_cases_match_visible_cases_and_independent_oracles(task, fixture):
    namespace, recorded = harness_globals(task)
    baseline = runpy.run_path(str(fixture / "rollup.py"))
    # Run a trusted copy of the visible tests to inspect its case list.
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "visible.py"
        source = task["snapshot"]["files"]["test_rollup.py"].replace(
            "from rollup import summarize", "summarize = None"
        )
        script.write_text(source)
        visible = runpy.run_path(str(script))
    assert visible["visible_cases"]() == namespace["visible_cases"]()
    assert len(recorded["oracle_cases"]) == 68
    assert recorded["require_pure_inputs"] is True
    for args, expected in recorded["oracle_cases"]:
        before = copy.deepcopy(args)
        assert baseline["summarize"](*args) == namespace["oracle"](*args) == expected
        assert args == before


def test_baseline_candidate_is_correct_and_near_one(task, fixture):
    result = verify(fixture, small_args(task))
    assert result.performance.correctness is True, result.feedback
    assert result.score == 0 and not result.passed
    assert 0.5 < result.performance.median_ratio < 1.7
    assert len(result.performance.warmup) == 1
    assert len(result.performance.samples) == 7
    assert [s.order for s in result.performance.samples] == [
        "baseline-first" if index % 2 == 0 else "candidate-first" for index in range(7)
    ]


def test_algorithmic_reference_has_generous_timing_floor(task, fixture):
    (fixture / "rollup.py").write_text(reference("algorithmic"))
    result = verify(fixture, small_args(task))
    assert result.performance.correctness is True, result.feedback
    assert result.passed and result.performance.median_ratio >= 2


@pytest.mark.parametrize(
    "old,new",
    [
        ('"status": 503', '"status": 200'),
        ("timedelta(days=1)", "timedelta(days=0)"),
        ("timedelta(days=13)", "timedelta(days=14)"),
        ("low <= day <= high", "low < day < high"),
        ("if rid in metadata and low <= day <= high:", "if rid in metadata:"),
        (
            'avail[(rid, day)] = row["has_availability"]',
            'avail.setdefault((rid, day), row["has_availability"])',
        ),
        (
            'forecasts[(rid, day)] = (row["sunny_score"], row["is_good"])',
            'forecasts.setdefault((rid, day), (row["sunny_score"], row["is_good"]))',
        ),
        ("if not available:", "if False:"),
        ("forecasts.get(key, (0, False))", "forecasts.get(key, (100, True))"),
        (
            'record["best_score"] = max(record["best_score"], score)',
            'record["best_score"] = max(record["best_score"], score if good else 0)',
        ),
        ('record["good_days"] += 1', 'record["good_days"] = 1'),
        (
            'regions[metadata[rid]]["open_parks"] += 1',
            'regions[metadata[rid]]["open_parks"] += 2',
        ),
        ('(-r["best_score"], r["region"])', '(r["best_score"], r["region"])'),
        ('(-r["best_score"], r["region"])', '(-r["best_score"], r["region"].lower())'),
        ('"count": len(output)', '"count": len(campgrounds)'),
        ('regions[region]["parks"] += 1', 'regions[region]["parks"] = 1'),
        (
            'row["sunny_score"], row["is_good"]',
            'float(row["sunny_score"]), row["is_good"]',
        ),
    ],
)
def test_wrong_variants_earn_no_credit(task, fixture, old, new):
    source = reference("algorithmic")
    assert old in source
    (fixture / "rollup.py").write_text(source.replace(old, new))
    result = verify(fixture, small_args(task))
    assert result.performance.correctness is False, result.feedback
    assert not result.passed and result.score == 0
    assert not result.feedback.startswith("[harness error]")


def test_visible_file_poisoning_and_forged_result_do_not_pass(task, fixture):
    (fixture / "test_rollup.py").write_text("raise SystemExit(0)\n")
    (fixture / "_baseline_rollup.py").write_text("def summarize(*args): return {}\n")
    (fixture / "_speedup_harness.py").write_text("raise SystemExit(0)\n")
    forged = json.dumps({"ok": True, "speedup": 10000, "status": "ok", "nonce": "fake"})
    (fixture / "rollup.py").write_text(
        f"print({forged!r})\ndef summarize(*args):\n    return {{}}\n"
    )
    result = verify(fixture, small_args(task))
    assert result.performance.correctness is False, result.feedback
    assert result.score == 0 and not result.passed


def test_baseline_cannot_forge_a_faster_score(task, fixture):
    forged = json.dumps(
        {"ok": True, "speedup": 10**12, "status": "ok", "nonce": "fake"}
    )
    (fixture / "rollup.py").write_text(
        f"print({forged!r})\n" + task["snapshot"]["files"]["rollup.py"]
    )
    result = verify(fixture, small_args(task))
    assert result.performance.correctness is True, result.feedback
    assert result.score == 0 and not result.passed
    assert 0.5 < result.performance.median_ratio < 1.7


def test_correct_oracles_with_wrong_timed_output_earn_zero(task, fixture):
    source = reference("algorithmic").replace(
        'return {"count": len(output), "regions": output}',
        'if len(campgrounds) > 50:\n        output[0]["good_days"] += 1\n'
        '    return {"count": len(output), "regions": output}',
    )
    (fixture / "rollup.py").write_text(source)
    result = verify(fixture, small_args(task))
    assert result.performance.correctness is False, result.feedback
    assert "benchmark pair output mismatch" in result.feedback
    assert result.score == 0 and not result.passed


@pytest.mark.parametrize(
    "field,value",
    [
        ("fixture_version", "campsites-rollup-seeded-v2"),
        ("harness_args", [181]),
        ("seed", 1234),
        ("pass_threshold", 1),
        ("buckets", [[3, 1 / 3], [10, 2 / 3], [50, 1]]),
    ],
)
def test_task_configuration_changes_invalidate_cached_grading(task, field, value):
    old = TaskSpec.model_validate(task).verifier
    new = old.model_copy(deep=True)
    new.args[field] = value
    assert _verifier_cache_repr(old) != _verifier_cache_repr(new)


@pytest.mark.parametrize("phase", ["oracle", "benchmark"])
def test_correct_output_with_input_mutation_is_rejected(task, fixture, phase):
    condition = "True" if phase == "oracle" else "len(campgrounds) > 50"
    source = reference("algorithmic").replace(
        'return {"count": len(output), "regions": output}',
        f"if {condition}:\n        campgrounds.clear()\n"
        '    return {"count": len(output), "regions": output}',
    )
    (fixture / "rollup.py").write_text(source)
    result = verify(fixture, small_args(task))
    assert result.performance.correctness is False, result.feedback
    assert "mutated" in result.feedback
    assert result.score == 0 and not result.passed


def test_mutating_baseline_is_a_harness_error(task, fixture):
    args = small_args(task)
    args["baseline"]["source"] = args["baseline"]["source"].replace(
        'return {"count": len(output), "regions": output}',
        'campgrounds.clear()\n    return {"count": len(output), "regions": output}',
    )
    result = verify(fixture, args)
    assert result.feedback.startswith("[harness error]")
    assert "baseline mutated" in result.feedback
    assert result.performance.correctness is None


@pytest.mark.parametrize("candidate", ["micro", "algorithmic"])
def test_reference_preserves_inputs_and_matches_unseen_seeds(task, candidate):
    namespace, _ = harness_globals(task)
    implementation = runpy.run_path(str(TASK_DIR / "reference" / (candidate + ".py")))
    for seed in range(80, 110):
        args = namespace["make_input"](seed, size=31)
        before = copy.deepcopy(args)
        assert implementation["summarize"](*args) == namespace["oracle"](*args)
        assert args == before
