import pytest  # noqa: F401

from bench.report import render_leaderboard


def test_report_has_per_class_qualification_and_tombstone():
    md = render_leaderboard(
        per_class={
            "cheap/x": {
                "config-plumbing": {
                    "pass1": 0.9,
                    "cost": 1.0,
                    "tier": "one-shots",
                    "qualifies": True,
                }
            }
        },
        anchors={
            "anthropic/claude-sonnet-4.6": {
                "config-plumbing": {"pass1": 0.9, "cost": 12.0}
            }
        },
        frontier={"config-plumbing": ["cheap/x"]},
        retired=[
            {
                "id": "old/y",
                "reason": "flunked config",
                "date": "2026-06-01",
                "pass1": 0.3,
                "cost": 0.5,
            }
        ],
    )
    assert "## Budget tier" in md
    assert "cheap/x" in md and "config-plumbing" in md
    assert "## Retired" in md and "old/y" in md and "flunked config" in md


def _agentic_stats(**over):
    base = {
        "n": 7,
        "pass_rate": 1.0,
        "floor_n": 5,
        "floor_pass": 5,
        "floor_failed": [],
        "qualified": True,
        "hard_n": 2,
        "hard_pass": 2,
        "mean_tokens": 6500,
        "mean_turns": 4.0,
        "mean_latency_ms": 30000,
        "cost": 0.0009,
        "cost_per_solve": 0.0009,
        "tool_ok_rate": 1.0,
    }
    base.update(over)
    return base


def test_agentic_gate_splits_qualified_and_disqualified():
    md = render_leaderboard(
        per_class={},
        anchors={},
        frontier={},
        retired=[],
        agentic={
            "cheap/strong": _agentic_stats(hard_pass=2, cost=0.0009),
            "cheap/weakhard": _agentic_stats(hard_pass=0, cost=0.0005),
            "flunker": _agentic_stats(
                qualified=False, floor_pass=3, floor_failed=["slo-budget-breach-01"]
            ),
        },
    )
    assert "## Agentic leaderboard: qualified" in md
    assert "## Agentic leaderboard: disqualified" in md
    q = md.index("## Agentic leaderboard: qualified")
    dq = md.index("## Agentic leaderboard: disqualified")
    # The flunker sits in the disqualified section with its failed floor task.
    assert md.index("flunker") > dq
    assert "slo-budget-breach-01" in md
    # Among the qualified, more hard passes rank first even at higher cost.
    assert q < md.index("cheap/strong") < dq
    assert md.index("cheap/strong") < md.index("cheap/weakhard") < dq


def test_agentic_section_present_when_empty():
    md = render_leaderboard(per_class={}, anchors={}, frontier={}, retired=[])
    assert "## Agentic leaderboard: qualified" in md
    assert "No qualified models yet." in md
    # The ceiling section header is fixed and emitted even with no anchors.
    assert "## Frontier ceiling (agentic)" in md
    assert "No anchor ceiling results yet." in md


def test_anchors_go_to_ceiling_not_candidate_tables():
    md = render_leaderboard(
        per_class={},
        anchors={},
        frontier={},
        retired=[],
        agentic={
            "cheap/strong": _agentic_stats(hard_pass=2, cost=0.0009),
            # An anchor: cost 0 (free), would top the cost-ranked candidate table if not
            # split out. It must live in the ceiling section instead.
            "anthropic/claude-opus-4.8": _agentic_stats(
                hard_pass=2, cost=0.0, cost_per_solve=0.0, mean_latency_ms=42000
            ),
        },
        agentic_anchor_ids={"anthropic/claude-opus-4.8"},
    )
    ceiling = md.index("## Frontier ceiling (agentic)")
    budget = md.index("## Budget tier")
    # The anchor appears only in the ceiling section, after the candidate tables.
    assert md.index("anthropic/claude-opus-4.8") > ceiling
    assert ceiling < md.index("anthropic/claude-opus-4.8") < budget
    # The candidate stays in the qualified table above the ceiling.
    assert md.index("cheap/strong") < ceiling
    # Wall-time shown for the ceiling row (42000ms -> 42.0s); no cost column.
    assert "42.0" in md


def test_all_results_shows_non_qualifiers():
    # A model that does not qualify (too pricey / below bar) is absent from the
    # Budget tier table but must still appear under All results.
    md = render_leaderboard(
        per_class={
            "pricey/z": {
                "config-plumbing": {
                    "pass1": 0.5,
                    "cost": 9.0,
                    "tier": "needs-repair",
                    "qualifies": False,
                }
            }
        },
        anchors={},
        frontier={},
        retired=[],
    )
    assert "## All results" in md
    assert "pricey/z" in md
    assert (
        "No qualifying budget candidates yet." in md
    )  # correctly excluded from budget tier
