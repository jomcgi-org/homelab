from types import SimpleNamespace

import pytest  # noqa: F401

from bench.pareto import ClassScore, aggregate_by_class, pareto_frontier, qualifies


def test_frontier_flags_dominated():
    # (model, pass1, cost) - B dominated by A (worse quality, higher cost)
    pts = {"A": (0.9, 1.0), "B": (0.8, 2.0), "C": (0.95, 5.0)}
    front = pareto_frontier(pts)  # higher pass1 better, lower cost better
    assert "A" in front and "C" in front and "B" not in front


def test_qualifies_relative_to_anchor():
    anchor = ClassScore(pass1=0.8, cost=10.0)
    cand = ClassScore(pass1=0.85, cost=2.0)
    assert qualifies(cand, anchor)  # >= anchor pass1 and cheaper
    assert not qualifies(ClassScore(pass1=0.7, cost=1.0), anchor)  # below bar


def test_aggregate_by_class_skips_harness_errors():
    graded = SimpleNamespace(
        task_id="t",
        model_id="m",
        first_attempt_passed=True,
        outcome="pass@1",
        cost_usd=1.0,
        total_latency_ms=10,
        is_harness_error=False,
    )
    errored = SimpleNamespace(
        task_id="t",
        model_id="m",
        first_attempt_passed=False,
        outcome="fail",
        cost_usd=99.0,
        total_latency_ms=999,
        is_harness_error=True,
    )
    only_errored = SimpleNamespace(
        task_id="t",
        model_id="all-error",
        first_attempt_passed=False,
        outcome="fail",
        cost_usd=50.0,
        total_latency_ms=500,
        is_harness_error=True,
    )

    aggregate = aggregate_by_class([graded, errored, only_errored], {"t": "code-fix"})
    score = aggregate["m"]["code-fix"]

    assert score == ClassScore(pass1=1.0, pass2=1.0, cost=1.0, latency_ms=10.0)
    assert "all-error" not in aggregate
