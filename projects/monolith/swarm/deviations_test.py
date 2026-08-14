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
