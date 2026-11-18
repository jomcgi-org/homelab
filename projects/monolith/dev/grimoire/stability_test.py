"""Stability evidence must cover every run, check, and pinned source input."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import stability
from rehearsal_report import new_report

SOURCE = {
    "sha": "test-head",
    "dirty": False,
    "playwright": "pinned-playwright",
    "browser": "pinned-chromium",
}


def complete_report():
    report = new_report("session", 390)
    report.update(
        passed=True,
        source=dict(SOURCE),
        checks=list(
            json.loads((stability.HERE / "budgets.json").read_text())["session"]
        ),
    )
    return report


def write_report(directory, report):
    (directory / "report.json").write_text(json.dumps(report))


def test_requires_twenty_consecutive_full_sessions(tmp_path, monkeypatch, capsys):
    attempts = []

    def launch(directory):
        attempts.append(int(directory.name))
        assert not (directory / "report.json").exists()
        # Until all twenty reports pass, the summary must not show a success.
        markdown = (tmp_path / "report.md").read_text()
        assert f"Incomplete: {len(attempts) - 1}/20" in markdown
        write_report(directory, complete_report())
        return 0

    monkeypatch.setattr(stability, "rehearse", launch)
    assert stability.run_stability(tmp_path, SOURCE)
    assert attempts == list(range(1, 21))
    summary = json.loads((tmp_path / "report.json").read_text())
    assert summary["passed"] is True
    assert summary["source"] == SOURCE
    assert len(summary["runs"]) == summary["required_runs"] == 20
    assert "Stability session 390px 20/20: passed" in capsys.readouterr().out
    markdown = (tmp_path / "report.md").read_text()
    assert "Passed: 20/20 consecutive sessions passed" in markdown
    for value in (SOURCE["sha"], SOURCE["playwright"], SOURCE["browser"]):
        assert f"<code>{value}</code>" in markdown
    for attempt in attempts:
        assert (
            f"| {attempt} | Passed | <code>{attempt}/report.json</code> |" in markdown
        )


def test_stops_at_first_failure_without_retrying(tmp_path, monkeypatch, capsys):
    attempts = []

    def launch(directory):
        attempts.append(int(directory.name))
        report = complete_report()
        if len(attempts) == 7:
            report["passed"] = False
            report["failed_checks"] = [
                {
                    "check": "Concurrent sheet edits",
                    "observed": "Expected 20, got 18\ntrace",
                }
            ]
        write_report(directory, report)
        return 0 if report["passed"] else 1

    monkeypatch.setattr(stability, "rehearse", launch)
    assert not stability.run_stability(tmp_path, SOURCE)
    assert attempts == list(range(1, 8))
    summary = json.loads((tmp_path / "report.json").read_text())
    assert summary["passed"] is False
    assert len(summary["runs"]) == 7
    assert "FAIL Concurrent sheet edits: Expected 20, got 18" in capsys.readouterr().out
    markdown = (tmp_path / "report.md").read_text()
    assert "Failed: 6/20 consecutive sessions passed" in markdown
    assert "| 7 | Failed |" in markdown
    assert "| 8 |" not in markdown
    assert "Concurrent sheet edits: Expected 20, got 18</code>" in markdown


def test_old_evidence_cannot_certify_a_new_run(tmp_path, monkeypatch):
    (tmp_path / "1").mkdir()
    write_report(tmp_path / "1", complete_report())
    write_report(tmp_path, {"passed": True, "required_runs": 20})
    monkeypatch.setattr(stability, "rehearse", lambda directory: 0)
    assert not stability.run_stability(tmp_path, SOURCE)
    summary = json.loads((tmp_path / "report.json").read_text())
    assert summary["passed"] is False
    assert len(summary["runs"]) == 1


def test_interrupted_run_clears_a_previous_success_summary(tmp_path, monkeypatch):
    (tmp_path / "report.md").write_text("Passed: 20/20 consecutive sessions passed")
    write_report(tmp_path, {"passed": True, "required_runs": 20})

    def interrupted(directory):
        raise RuntimeError("Launcher interrupted")

    monkeypatch.setattr(stability, "rehearse", interrupted)
    with pytest.raises(RuntimeError, match="Launcher interrupted"):
        stability.run_stability(tmp_path, SOURCE)
    assert "Incomplete: 0/20" in (tmp_path / "report.md").read_text()
    summary = json.loads((tmp_path / "report.json").read_text())
    assert summary["passed"] is False
    assert summary["runs"] == []


@pytest.mark.parametrize(
    "defect",
    [
        "missing-check",
        "duplicate-check",
        "wrong-width",
        "wrong-mode",
        "stale-sha",
        "dirty",
        "other-playwright",
        "other-browser",
        "console",
        "page-error",
        "slow-request",
        "capture",
        "failure",
        "failed-verdict",
        "exit-code",
    ],
)
def test_rejects_incomplete_or_wrong_runtime_evidence(tmp_path, defect):
    report = complete_report()
    if defect == "missing-check":
        report["checks"].pop()
    elif defect == "duplicate-check":
        report["checks"][0] = report["checks"][1]
    elif defect == "wrong-width":
        report["width"] = 1280
    elif defect == "wrong-mode":
        report["mode"] = "join-links"
    elif defect == "stale-sha":
        report["source"]["sha"] = "previous-head"
    elif defect == "dirty":
        report["source"]["dirty"] = True
    elif defect == "other-playwright":
        report["source"]["playwright"] = "other-version"
    elif defect == "other-browser":
        report["source"]["browser"] = "local-chromium"
    elif defect == "console":
        report["console_errors"] = ["Unexpected console error"]
    elif defect == "page-error":
        report["errors"] = ["Unhandled page exception"]
    elif defect == "slow-request":
        report["slow_requests"] = [{"method": "GET", "url": "/lobby", "seconds": 3}]
        report["request_budget_seconds"] = 2
    elif defect == "capture":
        report["capture_errors"] = ["Trace missing"]
    elif defect == "failure":
        report["failure"] = "Journey stopped"
    elif defect == "failed-verdict":
        report["passed"] = False
    write_report(tmp_path, report)
    assert stability.check_run(tmp_path, 1 if defect == "exit-code" else 0, SOURCE)


@pytest.mark.parametrize("timeout", [False, True])
def test_launches_the_ci_session_command_and_cleans_up_on_timeout(
    tmp_path, monkeypatch, timeout
):
    calls = []

    class Child:
        def __init__(self, command, **kwargs):
            calls.append(command)
            self.terminated = False

        def wait(self, timeout):
            if not self.terminated and timeout == 960 and test_timeout:
                raise subprocess.TimeoutExpired("run.py", timeout)
            return 0

        def terminate(self):
            self.terminated = True
            calls.append("terminated")

    test_timeout = timeout
    monkeypatch.setattr(stability.subprocess, "Popen", Child)
    assert stability.rehearse(tmp_path) == (124 if timeout else 0)
    assert calls[0] == [
        sys.executable,
        str(Path(stability.__file__).with_name("run.py")),
        "--rehearse",
        "--viewport",
        "390",
        "--output",
        str(tmp_path),
    ]
    assert ("terminated" in calls) is timeout
