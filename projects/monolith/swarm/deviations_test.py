import pytest

from swarm import deviations
from swarm.deviations import compute_deviations


def plan(**overrides):
    value = {
        "pinned": True,
        "max_attempts": 2,
        "implementer_model": "luna",
        "reviewer_model": "opus",
        "budget_usd": 1.0,
    }
    value.update(overrides)
    return value


def node(key, attempts=None, verdict=None, label=None):
    return {
        "key": key,
        "label": label or key,
        "attempts": attempts or [],
        "verdict": verdict,
    }


def attempt(model="luna", finding=None):
    return {"model": model, "finding": finding}


def test_attempts_exhausted():
    run = {
        "state": "escalated",
        "plan": plan(),
        "nodes": [node("implement", [attempt(), attempt()])],
    }
    assert [d["code"] for d in compute_deviations(run)] == [
        "attempts_exhausted",
        "retry_taken",
    ]
    assert "2" in compute_deviations(run)[0]["evidence"]


def test_retry_taken_includes_recorded_finding_code():
    run = {
        "plan": plan(),
        "nodes": [
            node(
                "implement",
                [attempt(finding={"code": "head_unchanged"}), attempt()],
            )
        ],
    }
    deviation = compute_deviations(run)[0]
    assert deviation["code"] == "retry_taken"
    assert "head_unchanged" in deviation["evidence"]


def test_model_mismatch():
    run = {
        "plan": plan(),
        "nodes": [node("review", [attempt(model="sonnet")])],
    }
    deviation = compute_deviations(run)[0]
    assert deviation["code"] == "model_mismatch"
    assert "sonnet" in deviation["evidence"]
    assert "opus" in deviation["evidence"]


def test_budget_exceeded():
    run = {"plan": plan(), "cost_usd": 1.25, "nodes": []}
    deviation = compute_deviations(run)[0]
    assert deviation["code"] == "budget_exceeded"
    # Formatted, not raw: cost_usd is a sum of floats and the client renders
    # these strings verbatim, so a bare 0.30000000000000004 would reach the page.
    assert "$1.25" in deviation["evidence"]
    assert "$1.00" in deviation["evidence"]
    assert "$1.25" in deviation["text"]


def test_budget_raise_prevents_exceeded_deviation():
    run = {
        "plan": plan(),
        "cost_usd": 1.25,
        "attributes": {"budget_raises": [{"from": 1.0, "to": 1.5}]},
        "nodes": [],
    }

    assert compute_deviations(run) == []


def test_pin_dependent_deviations_are_absent_when_plan_is_unpinned():
    run = {
        "state": "escalated",
        "plan": plan(pinned=False),
        "cost_usd": 2.0,
        "nodes": [
            node("implement", [attempt(), attempt(model="sonnet")]),
            node("review", [attempt(model="sonnet")]),
        ],
    }
    codes = {deviation["code"] for deviation in compute_deviations(run)}
    assert "attempts_exhausted" not in codes
    assert "model_mismatch" not in codes
    assert "budget_exceeded" not in codes
    assert "retry_taken" in codes


def test_non_approve_verdict_is_not_a_deviation():
    """A deviation is a departure from what the pinned plan promised.

    A reviewer asking for changes departs from nothing the plan promised, so
    it is the run's outcome, not a deviation. It is already stated by the
    disposition; a third restatement here was a category error.
    """
    run = {
        "plan": plan(),
        "nodes": [node("review", verdict={"value": "unparseable"})],
    }
    assert [d["code"] for d in compute_deviations(run)] == []


def factory_node(node_key, **overrides):
    node = {"node_key": node_key, "deps": [], "max_attempts": 2}
    node.update(overrides)
    return node


def factory_run(node_key, status="succeeded"):
    return {"node_key": node_key, "status": status}


def deviation(nodes, runs, **kwargs):
    values = {"review_rounds_used": 0, "max_review_rounds": 2}
    values.update(kwargs)
    return deviations.factory_deviation(nodes, runs, **values)


def test_every_deviation_is_named_and_known():
    # The reconciler asks only when nothing is ready, so there is no None
    # answer to handle and every answer is one of the declared codes.
    result = deviation([factory_node("conductor_1")], [factory_run("conductor_1")])
    assert result["code"] == "initial_plan" and result["node_key"] == "run"
    assert result["code"] in deviations.FACTORY_DEVIATION_CODES


def test_exhausted_review_rounds_outrank_an_exhausted_graph():
    nodes = [factory_node("implement_fix"), factory_node("review_fix")]
    runs = [factory_run("implement_fix"), factory_run("review_fix")]
    result = deviation(nodes, runs, review_rounds_used=2, pending_review="review_fix")
    assert result["code"] == "review_rounds_exhausted"
    assert result["node_key"] == "review_fix"


def test_an_unresolved_review_within_its_rounds_is_not_yet_exhausted():
    nodes = [factory_node("implement_fix"), factory_node("review_fix")]
    runs = [factory_run("implement_fix"), factory_run("review_fix")]
    result = deviation(nodes, runs, review_rounds_used=1, pending_review="review_fix")
    assert result["code"] == "graph_exhausted"


def test_a_refused_round_is_named_before_anything_else():
    result = deviation(
        [], [], pending_review="review_fix", loop_refusal="duplicate_key"
    )
    assert result["code"] == "loop_insert_refused"
    assert "duplicate_key" in result["evidence"]


@pytest.mark.parametrize(
    "status, code", [("failed", "node_failed"), ("escalated", "node_escalated")]
)
def test_a_settled_node_with_no_runnable_retry_reaches_the_planner(status, code):
    nodes = [factory_node("implement_fix", max_attempts=1)]
    result = deviation(nodes, [factory_run("implement_fix", status)])
    assert result["code"] == code and result["node_key"] == "implement_fix"


def test_a_succeeded_node_is_never_reported_as_failed():
    nodes = [factory_node("implement_fix")]
    runs = [factory_run("implement_fix", "failed"), factory_run("implement_fix")]
    assert deviation(nodes, runs)["code"] == "graph_exhausted"


def test_a_settled_graph_without_delivery_reaches_the_planner():
    nodes = [factory_node("implement_fix"), factory_node("review_fix")]
    runs = [factory_run("implement_fix"), factory_run("review_fix")]
    result = deviation(nodes, runs)
    assert result["code"] == "graph_exhausted"
    assert set(deviations.FACTORY_DEVIATION_CODES) >= {result["code"]}
