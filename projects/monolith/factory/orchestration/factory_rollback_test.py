"""Offline rollback policy and recovery regressions."""

import pytest

from factory.orchestration.factory_controls import rollback_policy


def test_default_off():
    assert rollback_policy({}) == {
        "enabled": False,
        "window_minutes": 15,
        "max_per_hour": 2,
        "charts": {},
    }


def test_chart_override():
    policy = rollback_policy(
        {
            "rollback": {
                "charts": {
                    "demo": {"window_minutes": 7, "synthetic_signal": "demo.ready"}
                }
            }
        }
    )
    assert policy["charts"]["demo"] == {
        "window_minutes": 7,
        "synthetic_signal": "demo.ready",
    }
    assert not policy["enabled"]


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        {"enabled": 1},
        {"window_minutes": True},
        {"window_minutes": 0},
        {"max_per_hour": 3},
        {"unknown": False},
        {"charts": {"demo": {"synthetic_signal": ""}}},
        {"charts": {"demo": {"enabled": True}}},
    ],
)
def test_invalid_policy(value):
    with pytest.raises(ValueError):
        rollback_policy({"rollback": value})
