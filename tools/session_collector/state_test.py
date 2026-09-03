import os

from tools.session_collector.state import eligible, forget, load, save


def test_quiet_threshold_and_grown_file_rule(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("old")
    os.utime(transcript, (100, 100))
    assert eligible(transcript, None, 30, now=2000) is True
    assert eligible(transcript, None, 30, now=1800) is False
    entry = {"status": "uploaded", "size": 3}
    assert eligible(transcript, entry, 30, now=2000) is False
    transcript.write_text("grown")
    os.utime(transcript, (100, 100))
    assert eligible(transcript, entry, 30, now=2000) is True
    assert eligible(transcript, {"status": "failed", "size": 5}, 30, now=2000)


def test_save_is_atomic(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    original_replace = os.replace
    observed = []

    def inspect_replace(source, destination):
        observed.append((source, destination))
        assert os.path.exists(source)
        assert os.path.dirname(source) == str(tmp_path)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", inspect_replace)
    save(state_file, {"session": {"status": "uploaded"}})
    assert observed
    assert load(state_file)["session"]["status"] == "uploaded"


def test_forget_removes_resolved_path(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("content")
    state_file = tmp_path / "state.json"
    save(state_file, {str(transcript.resolve()): {"status": "uploaded"}})
    assert forget(state_file, transcript) is True
    assert load(state_file) == {}
    assert forget(state_file, transcript) is False
