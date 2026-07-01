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
