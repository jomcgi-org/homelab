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


def test_agentic_section_renders_and_sorts_by_pass_rate():
    md = render_leaderboard(
        per_class={},
        anchors={},
        frontier={},
        retired=[],
        agentic={
            "cheap/win": {
                "n": 3,
                "pass_rate": 1.0,
                "med_tokens": 6500,
                "med_turns": 4.0,
                "cost": 0.0009,
                "tool_ok_rate": 1.0,
            },
            "pricey/lose": {
                "n": 3,
                "pass_rate": 0.33,
                "med_tokens": 19500,
                "med_turns": 9.0,
                "cost": 0.05,
                "tool_ok_rate": 0.66,
            },
        },
    )
    assert "## Agentic (tool-calling) leaderboard" in md
    assert "cheap/win" in md and "pricey/lose" in md
    # Higher pass-rate ranks first.
    assert md.index("cheap/win") < md.index("pricey/lose")


def test_agentic_section_present_when_empty():
    md = render_leaderboard(per_class={}, anchors={}, frontier={}, retired=[])
    assert "## Agentic (tool-calling) leaderboard" in md
    assert "No agentic results yet." in md


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
