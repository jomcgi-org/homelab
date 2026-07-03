"""Tests for stage-marker parsing in the goosecracker live-progress buffer.

Recipes emit marker lines on stdout (``::stage::<index>::<state>::<title>``
and ``::stages::<n>``) to announce structured plan progress alongside the
free-text tail. See ``goosecracker_progress.py`` for the grammar.
"""

from chat import goosecracker_progress as gp


def _fresh(artifact_id: str) -> None:
    gp.clear(artifact_id)


def test_single_stage_marker_adds_a_stage():
    _fresh("s1")
    gp.append("s1", "::stage::0::running::Fetch data\n")
    snap = gp.get("s1")
    assert len(snap.stages) == 1
    assert snap.stages[0].index == 0
    assert snap.stages[0].title == "Fetch data"
    assert snap.stages[0].state == "running"
    _fresh("s1")


def test_stage_marker_updates_state_in_place():
    _fresh("s2")
    gp.append("s2", "::stage::0::pending::Fetch data\n")
    gp.append("s2", "::stage::0::done::Fetch data\n")
    snap = gp.get("s2")
    assert len(snap.stages) == 1
    assert snap.stages[0].state == "done"
    _fresh("s2")


def test_stages_reset_discards_old_stages():
    _fresh("s3")
    gp.append("s3", "::stage::0::done::Old step\n")
    gp.append("s3", "::stages::2\n")
    gp.append("s3", "::stage::0::pending::New step A\n")
    gp.append("s3", "::stage::1::pending::New step B\n")
    snap = gp.get("s3")
    assert [s.title for s in snap.stages] == ["New step A", "New step B"]
    _fresh("s3")


def test_title_containing_double_colon_is_preserved():
    _fresh("s4")
    gp.append("s4", "::stage::0::running::Build a::b::c\n")
    snap = gp.get("s4")
    assert snap.stages[0].title == "Build a::b::c"
    _fresh("s4")


def test_malformed_marker_is_dropped_not_added_as_stage_or_text():
    _fresh("s5")
    gp.append("s5", "::stage::notanindex::running::Fetch data\n")
    gp.append("s5", "::stage::0::not-a-real-state::Fetch data\n")
    snap = gp.get("s5")
    assert snap.stages == []
    assert snap.text == ""
    _fresh("s5")


def test_marker_line_never_appears_in_text():
    _fresh("s6")
    gp.append("s6", "before\n::stage::0::running::Step 1\nafter\n")
    snap = gp.get("s6")
    assert snap.text == "before\nafter\n"
    _fresh("s6")


def test_plain_chunk_with_no_newline_appends_exactly_as_before():
    # Compatibility guard: chunks with no marker and no newline must still
    # append immediately, matching the pre-stage-marker behaviour, because
    # the artifact recipes render the raw tail unchanged.
    _fresh("s7")
    gp.append("s7", "hello ")
    gp.append("s7", "world")
    snap = gp.get("s7")
    assert snap.text == "hello world"
    assert snap.stages == []
    _fresh("s7")


def test_marker_split_across_two_append_calls_is_parsed_once():
    _fresh("s8")
    gp.append("s8", "::stage::0::run")
    # Not yet complete: nothing should be parsed or rendered yet.
    mid = gp.get("s8")
    assert mid.stages == []
    assert mid.text == ""
    gp.append("s8", "ning::Step 1\n")
    snap = gp.get("s8")
    assert len(snap.stages) == 1
    assert snap.stages[0].state == "running"
    assert snap.stages[0].title == "Step 1"
    assert snap.text == ""
    _fresh("s8")


def test_stages_version_bumps_on_change_and_not_on_no_op_repeat():
    _fresh("s9")
    gp.append("s9", "::stage::0::pending::Step 1\n")
    v1 = gp.get("s9").stages_version
    gp.append("s9", "::stage::0::running::Step 1\n")
    v2 = gp.get("s9").stages_version
    assert v2 > v1
    gp.append("s9", "::stage::0::running::Step 1\n")
    v3 = gp.get("s9").stages_version
    assert v3 == v2
    _fresh("s9")


def test_get_returns_isolated_copy_of_stages():
    _fresh("s10")
    gp.append("s10", "::stage::0::pending::Step 1\n")
    snap = gp.get("s10")
    gp.append("s10", "::stage::0::done::Step 1\n")
    # The earlier snapshot's stage list must not mutate underneath it.
    assert snap.stages[0].state == "pending"
    assert gp.get("s10").stages[0].state == "done"
    _fresh("s10")
