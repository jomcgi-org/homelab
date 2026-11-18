"""The browser gate must fail even when later journeys and evidence succeed."""

import json
from pathlib import Path

import pytest

from rehearsal_report import (
    CheckRecorder,
    aggregate_reports,
    declare_refused_poll,
    expect_network_diagnostic,
    finish_report,
    new_report,
    print_failure_summary,
    record_console,
)


def test_console_error_fails_a_completed_journey():
    report = new_report("session", 390)
    report["checks"].append("Complete journey")
    record_console(
        report,
        "Application failed",
        {"url": "http://friends.localhost:8177/grimoire/assets/app.js"},
        "a",
    )
    finish_report(report)
    assert report["passed"] is False
    assert report["console_errors"] == ["Application failed"]


def test_expected_network_diagnostic_needs_exact_request_and_browser_location():
    report = new_report("session", 390)
    url = "http://friends.localhost:8177/api/grimoire/example"
    text = "Failed to load resource: net::ERR_FAILED"
    expect_network_diagnostic(report, "a", "GET", url, failure="net::ERR_FAILED")
    report["network_failures"].append(
        {"seat": "a", "method": "GET", "url": url, "failure": "net::ERR_FAILED"}
    )
    record_console(report, text, {"url": url}, "a", argument_count=0)
    record_console(
        report,
        text,
        {"url": "http://friends.localhost:8177/grimoire/assets/app.js"},
        "a",
    )
    finish_report(report)
    assert report["passed"] is False
    assert len(report["expected_console_diagnostics"]) == 1
    assert report["console_errors"] == [text]


def test_application_console_error_at_resource_url_is_never_expected():
    report = new_report("session", 390)
    url = "http://friends.localhost:8177/api/grimoire/example"
    text = "Failed to load resource: the server responded with a status of 400 (Bad Request)"
    expect_network_diagnostic(report, "a", "GET", url, status=400)
    report["responses"].append(
        {"seat": "a", "method": "GET", "url": url, "status": 400}
    )
    record_console(
        report,
        text,
        {"url": url, "lineNumber": 50, "columnNumber": 1},
        "a",
        argument_count=1,
    )
    finish_report(report)
    assert report["passed"] is False
    assert report["expected_console_diagnostics"] == []
    assert report["console_errors"] == [text]


@pytest.mark.parametrize("mismatch", ["seat", "url", "unobserved", "extra"])
def test_expected_network_diagnostic_cannot_hide_an_extra_error(mismatch):
    report = new_report("join-links", 1280)
    url = "http://friends.localhost:8177/grimoire/join/accept/confirm"
    expect_network_diagnostic(report, "a", "POST", url, status=400)
    if mismatch != "unobserved":
        report["responses"].append(
            {"seat": "a", "method": "POST", "url": url, "status": 400}
        )
    text = "Failed to load resource: the server responded with a status of 400 (Bad Request)"
    record_console(
        report,
        text,
        {"url": url + ("?other=1" if mismatch == "url" else "")},
        "b" if mismatch == "seat" else "a",
        argument_count=0,
    )
    if mismatch == "extra":
        record_console(report, text, {"url": url}, "a", argument_count=0)
    finish_report(report)
    assert report["passed"] is False
    assert len(report["console_errors"]) == 1


@pytest.mark.parametrize(
    "seat, polled, method, status, passes",
    [
        ("a", "elowen", "GET", 404, True),  # the character seat a just lost
        ("a", "bram", "GET", 404, False),  # a character seat a still holds
        ("b", "elowen", "GET", 404, False),  # seat a's loss, seen on seat b
        ("a", "elowen", "POST", 404, False),
        ("a", "elowen", "GET", 500, False),
    ],
)
def test_only_a_lost_characters_refused_poll_is_accepted(
    seat, polled, method, status, passes
):
    sheets = "http://friends.localhost:8177/api/grimoire/c/characters/{}/sheets"
    url, refused = sheets.format(polled), {"a": {sheets.format("elowen")}, "b": set()}
    report = new_report("session", 1280)
    report["checks"].append("Character reassignment starts")
    declare_refused_poll(report, seat, method, url, status, refused[seat])
    report["responses"].append(
        {"seat": seat, "method": method, "url": url, "status": status}
    )
    text = f"Failed to load resource: the server responded with a status of {status}"
    record_console(report, text, {"url": url}, seat, argument_count=0)
    finish_report(report)
    assert report["passed"] is passes
    assert report["console_errors"] == ([] if passes else [text])


def test_step_budget_and_missing_step_are_independent_failures():
    report = new_report("session", 390)
    clock = iter([0, 1, 8])
    recorder = CheckRecorder(
        report, {"First": 3, "Second": 3, "Last": 3}, clock=lambda: next(clock)
    )
    report["checks"] = recorder
    recorder.append("First")
    recorder.append("Second")
    finish_report(report)
    assert report["passed"] is False
    assert report["steps"][1]["seconds"] == 7
    assert {row["check"] for row in report["failed_checks"]} == {
        "Second timing budget",
        "Last completed",
    }


def test_existing_failure_cannot_be_overwritten_by_clean_console():
    report = new_report("session", 390)
    report["passed"] = False
    finish_report(report)
    assert report["passed"] is False


def test_slow_request_fails_but_declared_delay_stays_visible():
    report = new_report("session", 390)
    report["request_budget_seconds"] = 1
    report["requests"] = [
        {"seat": "a", "method": "GET", "url": "/slow", "seconds": 2},
        {
            "seat": "b",
            "method": "GET",
            "url": "/injected",
            "seconds": 4,
            "injected": True,
        },
    ]
    finish_report(report)
    assert report["passed"] is False
    assert len(report["slow_requests"]) == 1
    assert report["slow_requests"][0]["url"] == "/slow"


def test_aggregate_requires_every_matrix_cell_and_validates_child_errors(tmp_path):
    cells = []
    for mode in ("session", "join-links"):
        for width in (1280, 390):
            directory = tmp_path / mode / str(width)
            directory.mkdir(parents=True)
            report = new_report(mode, width)
            report["passed"] = True
            report["checks"] = list(
                json.loads(Path(__file__).with_name("budgets.json").read_text())[mode]
            )
            if (mode, width) == ("session", 390):
                report["errors"] = ["Unhandled page error"]
            if (mode, width) != ("join-links", 390):
                (directory / "report.json").write_text(json.dumps(report))
            cells.append(
                {
                    "mode": mode,
                    "width": width,
                    "directory": str(directory),
                    "exit_code": 0,
                }
            )
    report = aggregate_reports(cells)
    assert report["passed"] is False
    assert len(report["failed_checks"]) == 2
    assert any("missing" in row["observed"] for row in report["failed_checks"])


def test_aggregate_rejects_missing_matrix_cell_even_when_all_children_pass(tmp_path):
    directory = tmp_path / "session"
    directory.mkdir()
    report = new_report("session", 1280)
    report["passed"] = True
    report["checks"] = list(
        json.loads(Path(__file__).with_name("budgets.json").read_text())["session"]
    )
    (directory / "report.json").write_text(json.dumps(report))
    aggregate = aggregate_reports(
        [
            {
                "mode": "session",
                "width": 1280,
                "directory": str(directory),
                "exit_code": 0,
            }
        ]
    )
    assert aggregate["passed"] is False
    assert len(aggregate["failed_checks"]) == 3


def test_matrix_logs_original_assertions_even_when_checks_did_not_complete(
    tmp_path, monkeypatch, capsys
):
    import run

    budgets = json.loads(Path(__file__).with_name("budgets.json").read_text())
    failed_names = [
        "Concurrent sheet edits merge without loss and stale saves are rejected",
        "Repeated Enter while campaign creation is pending creates exactly one campaign",
    ]
    # Drive the same --all entry point as ci.sh, replacing only its child
    # processes. A failing child still lets the other three cells run.
    launched = []

    class Child:
        def __init__(self, command, **kwargs):
            width = int(command[command.index("--viewport") + 1])
            mode = "join-links" if "--join-links" in command else "session"
            directory = Path(command[command.index("--output") + 1])
            report = new_report(mode, width)
            report["checks"] = list(budgets[mode])
            report["passed"] = (mode, width) != ("session", 390)
            self.status = 0 if report["passed"] else 1
            if self.status:
                for name, error in zip(
                    failed_names,
                    (
                        "Locator expected to have Value '20'",
                        "Locator expected to be visible",
                    ),
                ):
                    report["checks"].remove(name)
                    report["failed_checks"].append(
                        {
                            "check": name,
                            "observed": f"\n{error}\nCall log:\n  more detail",
                        }
                    )
            (directory / "report.json").write_text(json.dumps(report))
            launched.append((mode, width))

        def wait(self, timeout):
            return self.status

    (tmp_path / "initdb").touch()
    (tmp_path / "index.html").touch()
    monkeypatch.setenv("GRIMOIRE_PG_BIN", str(tmp_path))
    monkeypatch.setenv("GRIMOIRE_WEB_DIR", str(tmp_path))
    monkeypatch.setattr(run.subprocess, "Popen", Child)
    output = tmp_path / "evidence"
    monkeypatch.setattr(
        run.sys, "argv", ["run.py", "--rehearse", "--all", "--output", str(output)]
    )
    with pytest.raises(SystemExit, match="Grimoire journey matrix failed"):
        run.main()
    log = capsys.readouterr().out
    assert len(launched) == 4
    assert (
        f"FAIL session at 390px: {failed_names[0]}: Locator expected to have Value '20'"
        in log
    )
    assert (
        f"FAIL session at 390px: {failed_names[1]}: Locator expected to be visible"
        in log
    )
    assert "Required journey inventory is incomplete" in log
    assert "more detail" not in log
    artifact = json.loads((output / "report.json").read_text())
    assert artifact["passed"] is False
    assert "more detail" in artifact["failed_checks"][0]["observed"]


def test_failure_summary_includes_non_assertion_gate_failures(capsys):
    report = new_report("session", 390)
    report.update(
        errors=["Unhandled page error\nstack"],
        console_errors=["Unexpected console error\nstack"],
        capture_errors=["Could not save trace\nstack"],
        failure="Journey stopped\nstack",
        request_budget_seconds=2,
        slow_requests=[{"method": "GET", "url": "/lobby", "seconds": 3}],
    )
    print_failure_summary(report)
    assert capsys.readouterr().out.splitlines() == [
        "FAIL Page error: Unhandled page error",
        "FAIL Console error: Unexpected console error",
        "FAIL Evidence capture: Could not save trace",
        "FAIL Request budget: GET /lobby: 3.000s exceeds 2.000s",
        "FAIL Journey exception: Journey stopped",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"passed": True, "checks": []},
        {"passed": False, "checks": [], "failed_checks": [{}]},
        [],
    ],
)
def test_aggregate_rejects_incomplete_or_nonobject_child_report(tmp_path, payload):
    cells = []
    for mode in ("session", "join-links"):
        for width in (1280, 390):
            directory = tmp_path / mode / str(width)
            directory.mkdir(parents=True)
            value = (
                payload | {"mode": mode, "width": width}
                if isinstance(payload, dict)
                else payload
            )
            (directory / "report.json").write_text(json.dumps(value))
            cells.append(
                {
                    "mode": mode,
                    "width": width,
                    "directory": str(directory),
                    "exit_code": 0,
                }
            )
    result = aggregate_reports(cells)
    assert result["passed"] is False
    assert len(result["failed_checks"]) == 4


def test_required_journey_inventory_and_measured_budgets_remain_complete():
    budgets = json.loads(Path(__file__).with_name("budgets.json").read_text())
    assert len(budgets["session"]) == 53
    assert len(budgets["join-links"]) == 9
    assert len(budgets["baseline_steps"]["session"]) == 33
    assert len(budgets["baseline_steps"]["join-links"]) == 9
    assert budgets["request_seconds"] == 2
    assert all(
        seconds > 0
        for mode in ("session", "join-links")
        for seconds in budgets[mode].values()
    )
