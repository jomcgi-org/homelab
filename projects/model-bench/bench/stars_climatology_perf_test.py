"""Real task-pack grading smoke tests, with no model API or production imports."""

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

from bench.cli import _snapshot
from bench.schema import TaskSpec
from bench.verifiers.speedup import _bucket_score, verify

TASK_DIR = Path(__file__).resolve().parents[1] / "tasks/stars-climatology-perf-01"


@pytest.fixture
def task():
    # Intentionally fail on missing Bazel data rather than skip this smoke test.
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
    args["harness_args"] = [90]
    # Keep the real one-warmup/seven-pair protocol; only shrink the dataset.
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
    assert spec.target_files == args["editable"] == ["climatology.py"]
    assert set(task["snapshot"]["files"]) == {"climatology.py", "test_climatology.py"}
    assert (fixture / "climatology.py").read_bytes() == args["baseline"][
        "source"
    ].encode()
    assert args["protocol"] == "paired-v1" and args["pairs"] == 7
    assert args["harness_args"] == [220] and args["timeout_s"] == 120
    assert args["fixture_version"] == "stars-climatology-seeded-v1"
    assert spec.source_commit in args["baseline"]["source"]
    assert round(args["pass_threshold"], 12) == _bucket_score(2, args["buckets"])
    assert [_bucket_score(ratio, args["buckets"]) for ratio in (1.99, 2, 10, 50)] == [
        0,
        round(1 / 3, 12),
        round(2 / 3, 12),
        1,
    ]
    assert not any(path.name == "reference" for path in fixture.rglob("*"))


@pytest.mark.parametrize("candidate", ["baseline", "micro", "algorithmic"])
def test_visible_tests_run_on_each_correct_implementation(task, fixture, candidate):
    if candidate != "baseline":
        (fixture / "climatology.py").write_text(reference(candidate))
    result = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "-v", "test_climatology"],
        cwd=fixture,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stderr


def test_hidden_harness_protects_all_visible_cases(task, fixture, monkeypatch):
    monkeypatch.syspath_prepend(str(fixture))
    visible = runpy.run_path(str(fixture / "test_climatology.py"))
    namespace, recorded = harness_globals(task)
    cases = visible["visible_cases"]()
    assert recorded["oracle_cases"][: len(cases)] == cases
    assert len(recorded["oracle_cases"]) == len(cases) + 50
    build = namespace["make_input"]
    assert build(6696, size=40) == build(6696, size=40)
    assert build(6696, size=40) != build(6697, size=40)
    # Fixed-seed hidden cases check the production-derived baseline independently.
    baseline = runpy.run_path(str(fixture / "climatology.py"))
    for args, expected in recorded["oracle_cases"]:
        before = copy.deepcopy(args)
        assert baseline["summarize"](*args) == expected
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
    (fixture / "climatology.py").write_text(reference("algorithmic"))
    result = verify(fixture, small_args(task))
    assert result.performance.correctness is True, result.feedback
    assert result.passed and result.performance.median_ratio >= 2


@pytest.mark.parametrize(
    "old,new",
    [
        ('result["clear"][month - 1] +=', 'result["clear"][month - 1] ='),
        ('result["dark"][month - 1] +=', 'result["dark"][month - 1] ='),
        ("month - 1", "month % 12"),
        (
            "if not 1 <= month <= 12 or site_id not in metadata:",
            "if site_id not in metadata:",
        ),
        ('if sum(site["dark"]) > 0', "if True"),
        (
            'output.sort(key=lambda site: sum(site["clear"]), reverse=True)',
            'output.sort(key=lambda site: (-sum(site["clear"]), site["id"]))',
        ),
        (
            'output.sort(key=lambda site: sum(site["clear"]), reverse=True)',
            'output.sort(key=lambda site: sum(site["clear"]))',
        ),
        ('"count": len(output)', '"count": len(sites)'),
        ('"name": site["name"]', '"name": "wrong"'),
        ('"clear": [0] * 12', '"clear": [0] * 13'),
        (
            "if not 1 <= month <= 12 or site_id not in metadata:",
            'if not 1 <= month <= 12 or site_id not in metadata or row["dark_hours"] == 0:',
        ),
    ],
)
def test_wrong_variants_earn_no_credit(task, fixture, old, new):
    source = reference("algorithmic")
    assert old in source
    (fixture / "climatology.py").write_text(source.replace(old, new))
    result = verify(fixture, small_args(task))
    assert result.performance.correctness is False, result.feedback
    assert not result.passed and result.score == 0
    assert not result.feedback.startswith("[harness error]")


def test_visible_file_poisoning_and_forged_result_do_not_pass(task, fixture):
    (fixture / "test_climatology.py").write_text("raise SystemExit(0)\n")
    (fixture / "_baseline_climatology.py").write_text(
        "def summarize(*args): return {}\n"
    )
    (fixture / "_speedup_harness.py").write_text("raise SystemExit(0)\n")
    forged = json.dumps({"ok": True, "speedup": 10000, "status": "ok", "nonce": "fake"})
    (fixture / "climatology.py").write_text(
        f"print({forged!r})\ndef summarize(sites, rows):\n    return {{'sites': [], 'count': 0}}\n"
    )
    result = verify(fixture, small_args(task))
    assert result.performance.correctness is False, result.feedback
    assert result.score == 0 and not result.passed


@pytest.mark.parametrize("phase", ["oracle", "benchmark"])
def test_correct_output_with_input_mutation_is_rejected(task, fixture, phase):
    condition = "True" if phase == "oracle" else "len(sites) > 50"
    source = reference("algorithmic").replace(
        'return {"sites": output, "count": len(output)}',
        f"if {condition}:\n        sites.clear()\n"
        '    return {"sites": output, "count": len(output)}',
    )
    (fixture / "climatology.py").write_text(source)
    result = verify(fixture, small_args(task))
    assert result.performance.correctness is False, result.feedback
    assert "mutated" in result.feedback
    assert result.score == 0 and not result.passed


def test_mutating_baseline_is_a_harness_error(task, fixture):
    args = small_args(task)
    args["baseline"]["source"] = args["baseline"]["source"].replace(
        'return {"sites": output, "count": len(output)}',
        'sites.clear()\n    return {"sites": output, "count": len(output)}',
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
        want = namespace["oracle"](*args)
        assert implementation["summarize"](*args) == want
        assert args == before
