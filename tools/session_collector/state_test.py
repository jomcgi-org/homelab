import os
import threading

from tools.session_collector.__main__ import main
from tools.session_collector.state import eligible, forget, load, locked, save


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
    failed = {"status": "failed", "size": 5, "failures": 3}
    assert eligible(transcript, failed, 30, now=2000) is False
    transcript.write_text("grown again")
    os.utime(transcript, (100, 100))
    assert eligible(transcript, failed, 30, now=2000) is True
    assert failed["failures"] == 0


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


def test_forget_waits_for_collection_lock(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("content")
    state_file = tmp_path / "state.json"
    save(state_file, {str(transcript.resolve()): {"status": "uploaded"}})
    holding = threading.Event()
    release = threading.Event()
    result = []

    def hold_lock():
        with locked(state_file):
            holding.set()
            release.wait()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert holding.wait(timeout=1)
    forgetter = threading.Thread(
        target=lambda: result.append(forget(state_file, transcript))
    )
    forgetter.start()
    forgetter.join(timeout=0.05)
    assert forgetter.is_alive()
    release.set()
    holder.join(timeout=1)
    forgetter.join(timeout=1)
    assert result == [True]
    assert load(state_file) == {}


def test_status_reports_parked_failures(tmp_path, capsys):
    state_file = tmp_path / "state.json"
    save(
        state_file,
        {
            "retrying": {"status": "failed", "failures": 2},
            "parked": {"status": "failed", "failures": 3},
        },
    )
    assert main(["status", "--state-file", str(state_file)]) == 0
    output = capsys.readouterr().out
    assert "failed: 2" in output
    assert "failed (parked): 1" in output
