import pytest

from swarm.budget import effective_budget


def test_effective_budget_requires_a_pinned_budget():
    assert effective_budget({}) is None


def test_effective_budget_uses_pinned_budget_without_raises():
    assert effective_budget({"budget_usd": 1}) == 1.0


def test_effective_budget_uses_latest_raise():
    attributes = {"budget_raises": [{"to": 2}, {"to": 3.5}]}

    assert effective_budget({"budget_usd": 1}, attributes) == 3.5


@pytest.mark.parametrize(
    "budget_raises",
    ["invalid", [{}], [{"from": 1, "to": 2}, {"from": 2}]],
)
def test_effective_budget_ignores_invalid_latest_raise(budget_raises):
    assert effective_budget({"budget_usd": 1}, {"budget_raises": budget_raises}) == 1.0
