from swarm.rationale import parse_rationale


def test_clean_trailer_parses_areas_and_deviation():
    result = parse_rationale(
        "work done\nRATIONALE\n"
        "- area: swarm/rows.py · why: carries the final turn text\n"
        "- area: run view · why: keeps testimony with its attempt\n"
        "- deviation: left routing unchanged because rationale is display only"
    )
    assert result["parse_status"] == "parsed"
    assert result["areas"] == [
        {"area": "swarm/rows.py", "why": "carries the final turn text"},
        {"area": "run view", "why": "keeps testimony with its attempt"},
    ]
    assert result["deviations"] == [
        "left routing unchanged because rationale is display only"
    ]
    assert result["parser_version"] == 1


def test_markdown_decoration_is_tolerated():
    result = parse_rationale(
        "## RATIONALE\n* - area: view · why: show it\n* - deviation: none"
    )
    assert result["parse_status"] == "parsed"
    assert result["areas"] == [{"area": "view", "why": "show it"}]


def test_no_trailer_is_absent():
    assert parse_rationale("ordinary reply") == {
        "raw": None,
        "parse_status": "none",
        "areas": [],
        "deviations": [],
        "parser_version": 1,
    }


def test_garbage_header_preserves_raw():
    result = parse_rationale("RATIONALE\nthis is not a bullet")
    assert result["parse_status"] == "unparseable"
    assert result["raw"] == "RATIONALE\nthis is not a bullet"
    assert result["areas"] == []


def test_header_outside_tail_window_is_absent():
    assert (
        parse_rationale("RATIONALE\n- area: old · why: old\n" + "noise\n" * 12)[
            "parse_status"
        ]
        == "none"
    )


def test_area_without_why_does_not_invent_one():
    result = parse_rationale("RATIONALE\n- area: an untouched file")
    assert result["areas"] == [{"area": "an untouched file", "why": ""}]


def test_duplicated_blocks_fail_closed():
    result = parse_rationale(
        "RATIONALE\n- area: one · why: first\nRATIONALE\n- area: two · why: second"
    )
    assert result["parse_status"] == "unparseable"
    assert result["areas"] == []
