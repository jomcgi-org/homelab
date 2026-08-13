from swarm.rationale import parse_rationale


def test_clean_trailer_parses_paths_and_deviation():
    result = parse_rationale(
        "work done\nRATIONALE\n"
        "- path: swarm/rows.py · why: carries the final turn text\n"
        "- path: swarm/run_view.py · why: keeps testimony with its attempt\n"
        "- deviation: left routing unchanged because rationale is display only"
    )
    assert result["parse_status"] == "parsed"
    assert result["paths"] == [
        {"path": "swarm/rows.py", "why": "carries the final turn text"},
        {"path": "swarm/run_view.py", "why": "keeps testimony with its attempt"},
    ]
    assert result["deviations"] == [
        "left routing unchanged because rationale is display only"
    ]
    assert result["parser_version"] == 1


def test_markdown_decoration_is_tolerated():
    result = parse_rationale(
        "## RATIONALE\n* - path: swarm/view.py · why: show it\n* - deviation: none"
    )
    assert result["parse_status"] == "parsed"
    assert result["paths"] == [{"path": "swarm/view.py", "why": "show it"}]


def test_no_trailer_is_absent():
    assert parse_rationale("ordinary reply") == {
        "raw": None,
        "parse_status": "none",
        "paths": [],
        "deviations": [],
        "parser_version": 1,
    }


def test_garbage_header_preserves_raw():
    result = parse_rationale("RATIONALE\nthis is not a bullet")
    assert result["parse_status"] == "unparseable"
    assert result["raw"] == "RATIONALE\nthis is not a bullet"
    assert result["paths"] == []


def test_header_outside_tail_window_is_absent():
    assert (
        parse_rationale("RATIONALE\n- path: old.py · why: old\n" + "noise\n" * 12)[
            "parse_status"
        ]
        == "none"
    )


def test_path_without_why_does_not_invent_one():
    result = parse_rationale("RATIONALE\n- path: untouched/file.py")
    assert result["paths"] == [{"path": "untouched/file.py", "why": ""}]


def test_missing_or_absolute_path_fails_closed():
    for bullet in (
        "- path: · why: missing",
        "- path: /absolute/file.py · why: bad",
    ):
        result = parse_rationale(f"RATIONALE\n{bullet}")
        assert result["parse_status"] == "unparseable"
        assert result["paths"] == []


def test_duplicated_blocks_fail_closed():
    result = parse_rationale(
        "RATIONALE\n- path: one.py · why: first\nRATIONALE\n- path: two.py · why: second"
    )
    assert result["parse_status"] == "unparseable"
    assert result["paths"] == []
