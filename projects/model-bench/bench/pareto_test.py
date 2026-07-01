from bench.pareto import pareto_frontier, qualifies, ClassScore


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
