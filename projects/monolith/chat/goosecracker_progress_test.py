"""Tests for the in-process goosecracker live-progress buffer."""

from chat import goosecracker_progress as gp


def _fresh(artifact_id: str) -> None:
    gp.clear(artifact_id)


def test_append_accumulates_and_get_returns_snapshot():
    _fresh("t1")
    assert gp.get("t1") is None
    gp.append("t1", "hello ")
    gp.append("t1", "world")
    snap = gp.get("t1")
    assert snap is not None
    assert snap.text == "hello world"
    assert snap.done is False
    _fresh("t1")


def test_get_returns_copy_not_live_reference():
    _fresh("t2")
    gp.append("t2", "abc")
    snap = gp.get("t2")
    gp.append("t2", "def")
    # The earlier snapshot must not mutate when the buffer grows.
    assert snap.text == "abc"
    assert gp.get("t2").text == "abcdef"
    _fresh("t2")


def test_append_keeps_only_the_tail():
    _fresh("t3")
    gp.append("t3", "x" * (gp._MAX_BUFFER + 500))
    snap = gp.get("t3")
    assert len(snap.text) == gp._MAX_BUFFER
    _fresh("t3")


def test_empty_chunk_is_ignored():
    _fresh("t4")
    gp.append("t4", "")
    assert gp.get("t4") is None
    _fresh("t4")


def test_mark_done_sets_flag():
    _fresh("t5")
    gp.append("t5", "building")
    gp.mark_done("t5")
    snap = gp.get("t5")
    assert snap.done is True
    assert snap.text == "building"
    _fresh("t5")


def test_mark_done_without_prior_append_creates_entry():
    _fresh("t6")
    gp.mark_done("t6")
    snap = gp.get("t6")
    assert snap is not None
    assert snap.done is True
    _fresh("t6")


def test_clear_removes_entry():
    _fresh("t7")
    gp.append("t7", "data")
    gp.clear("t7")
    assert gp.get("t7") is None
