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


SHEETS = "http://friends.localhost:8177/api/grimoire/campaigns/c/characters/{}/sheets"
REFUSED_TEXT = (
    "Failed to load resource: the server responded with a status of 404 (Not Found)"
)


def refused_poll_report(seat, polled, refused_for_seat):
    """One session poll answered 404, as the reassignment journey observes it."""
    report = new_report("session", 1280)
    report["checks"].append("Character reassignment starts")
    declared = declare_refused_poll(
        report, seat, "GET", polled, 404, refused_for_seat
    )
    report["responses"].append(
        {"seat": seat, "method": "GET", "url": polled, "status": 404}
    )
    record_console(report, REFUSED_TEXT, {"url": polled}, seat, argument_count=0)
    finish_report(report)
    return declared, report


def test_refused_poll_for_the_lost_character_is_accepted():
    lost = SHEETS.format("elowen")
    declared, report = refused_poll_report("a", lost, {lost})
    assert declared is True
    assert report["passed"] is True
    assert report["console_errors"] == []
    assert len(report["expected_console_diagnostics"]) == 1


def test_refused_poll_for_a_character_the_seat_still_holds_fails():
    held = SHEETS.format("bram")
    declared, report = refused_poll_report("a", held, {SHEETS.format("elowen")})
    assert declared is False
    assert report["passed"] is False
    assert report["console_errors"] == [REFUSED_TEXT]


def test_refused_poll_declared_for_one_seat_fails_on_another():
    lost_by_a = SHEETS.format("elowen")
    report = new_report("session", 1280)
    report["checks"].append("Character reassignment starts")
    assert declare_refused_poll(report, "a", "GET", lost_by_a, 404, {lost_by_a})
    report["responses"].append(
        {"seat": "b", "method": "GET", "url": lost_by_a, "status": 404}
    )
    record_console(report, REFUSED_TEXT, {"url": lost_by_a}, "b", argument_count=0)
    finish_report(report)
    assert report["passed"] is False
    assert report["console_errors"] == [REFUSED_TEXT]


@pytest.mark.parametrize("method, status", [("POST", 404), ("GET", 403), ("GET", 500)])
def test_only_a_refused_get_is_declared(method, status):
    lost = SHEETS.format("elowen")
    report = new_report("session", 1280)
    assert declare_refused_poll(report, "a", method, lost, status, {lost}) is False
    assert report["expected_network_diagnostics"] == []


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


@pytest.mark.parametrize("payload", [{"passed": True, "checks": []}, []])
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
