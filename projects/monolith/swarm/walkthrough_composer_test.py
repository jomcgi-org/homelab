import json

from swarm.walkthrough_composer import compose_walkthrough


def _rationale(paths=(), deviations=()):
    return {
        "parse_status": "parsed",
        "paths": [{"path": path, "why": "because"} for path in paths],
        "deviations": list(deviations),
        "parser_version": 1,
    }


def _compare(files, rung=1, **extra):
    stats = extra.pop("stats", {"total_files": len(files)})
    return {
        "resolution_rung": rung,
        "files": files,
        "stats": stats,
        "unexplained_files": [],
        "contradicted_paths": [],
        **extra,
    }


def _file(path, classification="authored"):
    return {
        "path": path,
        "status": "modified",
        "additions": 2,
        "deletions": 1,
        "classification": classification,
    }


def test_rungs_one_and_two_are_full_and_two_is_ephemeral():
    files = [_file("a.py")]
    one = compose_walkthrough(1, 2, _compare(files), _rationale(["a.py"]), {})
    assert one["rung"] == 1
    assert one["ephemeral"] is False
    assert one["summary"] == {
        "status": "available",
        "provenance": "git_compare_and_agent_account",
        "files_changed": 1,
        "insertions": 2,
        "deletions": 1,
        "accounted_files": 1,
        "unexplained_files": 0,
    }
    two = compose_walkthrough(1, 2, _compare(files, rung=2), _rationale(["a.py"]), {})
    assert two["rung"] == 2
    assert two["ephemeral"] is True
    assert "branch is deleted" in two["message"]


def test_rung_three_has_testimony_and_activities_without_diff_stats():
    result = compose_walkthrough(
        1,
        2,
        None,
        _rationale(["a.py"], ["used a fallback"]),
        {
            "activities": [
                {"type": "edit", "file_path": "a.py"},
                {"type": "write", "file_path": "b.py"},
            ]
        },
    )
    assert result["rung"] == 3
    assert result["summary"]["status"] == "diff_unavailable"
    assert result["summary"]["provenance"] == "agent_account_only"
    assert result["message"].startswith("Limited walkthrough")
    assert {step.get("file_path") for step in result["steps"]} == {"a.py", "b.py"}
    assert {
        point.get("deviation")
        for point in result["steps"][0]["testimony"]["points"]
        if "deviation" in point
    } == {"used a fallback"}
    assert all(
        "file_change" not in step
        for step in result["steps"]
        if step.get("register") == "testimony"
    )


def test_rungs_four_and_five_decline_or_stop():
    activities = {
        "activities": [{"type": "edit", "file_path": f"f{i}.py"} for i in range(20)]
    }
    four = compose_walkthrough(1, 1, None, {"parse_status": "none"}, activities)
    assert four["rung"] == 4
    assert four["summary"]["status"] == "not_available"
    assert four["steps"] == []
    assert "decline" in four["message"]
    five = compose_walkthrough(1, 1, None, {"parse_status": "none"}, {})
    assert five["rung"] == 5
    assert five["summary"]["status"] == "not_available"
    assert five["message"] == "No activity recorded"


def test_mechanical_files_collapse_per_generator_run_and_authored_order_is_retained():
    files = [
        _file("a.py"),
        _file("one.out", "mechanical"),
        _file("two.out", "mechanical"),
        _file("three.out", "mechanical"),
    ]
    activities = {
        "activities": [
            {"type": "edit", "file_path": "a.py"},
            {
                "type": "run",
                "command": "ci regen",
                "produced_files": ["one.out", "two.out"],
            },
            {"type": "run", "command": "asset regen", "produced_files": ["three.out"]},
        ]
    }
    result = compose_walkthrough(1, 1, _compare(files), _rationale(), activities)
    mechanical = [step for step in result["steps"] if step["type"] == "mechanical"]
    assert [step["count"] for step in mechanical] == [2, 1]
    assert [step["generator_activity"]["command"] for step in mechanical] == [
        "ci regen",
        "asset regen",
    ]
    assert result["steps"][0]["file_path"] == "a.py"


def test_truncation_cross_checks_and_register_shape():
    files = [_file("a.py"), _file("generated.py", "mechanical")]
    compare = _compare(
        files,
        stats={"total_files": 300, "truncated_at": 300},
        unexplained_files=["a.py", "generated.py"],
        contradicted_paths=["missing.py"],
        activities_truncated=True,
    )
    result = compose_walkthrough(1, 1, compare, _rationale(["missing.py"]), {})
    assert [
        step["label"] for step in result["steps"] if step["type"] == "truncation"
    ] == ["GitHub files truncated", "activities truncated"]
    assert any(
        step["type"] == "unexplained" and step["file_path"] == "a.py"
        for step in result["steps"]
    )
    contradiction = next(
        step for step in result["steps"] if step["type"] == "contradiction"
    )
    assert contradiction["register"] == "testimony"
    assert contradiction["testimony"]["points"][0]["path"] == "missing.py"
    assert all("T" not in json.dumps(step) for step in result["steps"])


def test_composer_has_no_run_tier_filename_representation():
    result = compose_walkthrough(
        1, 1, _compare([_file("a.py")]), _rationale(["a.py"]), {}
    )
    # The session composer emits only session steps. A run view must link here,
    # not inline this payload, so no run-tier field is introduced accidentally.
    assert "run" not in result
