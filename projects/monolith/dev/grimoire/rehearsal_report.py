"""Evidence and failure accounting shared by the local browser rehearsals.

This module has no browser or application dependency so the gate itself can be
tested without Chromium, PostgreSQL, credentials or the production app.
"""

import json
import re
import time
from pathlib import Path

MATRIX = (
    ("session", 1280),
    ("session", 390),
    ("join-links", 1280),
    ("join-links", 390),
)


def new_report(mode, width):
    return {
        "simulated": True,
        "mode": mode,
        "width": width,
        "checks": [],
        "failed_checks": [],
        "errors": [],
        "timings": {},
        "steps": [],
        "requests": [],
        "responses": [],
        "console_errors": [],
        "console_events": [],
        "network_failures": [],
        "expected_network_diagnostics": [],
        "expected_console_diagnostics": [],
        "unavailable": [],
    }


class CheckRecorder(list):
    """Time the existing scenario's check boundaries without dropping checks."""

    def __init__(self, report, budgets, *, clock=time.monotonic):
        super().__init__()
        self.report = report
        self.budgets = budgets
        self.clock = clock
        self.last = clock()
        self.screenshot_count = len(report.get("screenshots", []))

    def ready(self):
        """Browser and seat setup has ended; interaction timing starts here."""
        self.last = self.clock()

    def append(self, check):
        now = self.clock()
        elapsed = now - self.last
        self.last = now
        budget = self.budgets.get(check)
        screenshots = self.report.get("screenshots", [])
        self.report["steps"].append(
            {
                "check": check,
                "seconds": elapsed,
                "budget_seconds": budget,
                "screenshots": [
                    row["file"] for row in screenshots[self.screenshot_count :]
                ],
            }
        )
        self.screenshot_count = len(screenshots)
        if budget is None:
            self.report["failed_checks"].append(
                {
                    "check": f"{check} timing budget",
                    "observed": "No measured budget is registered for this check",
                }
            )
        elif elapsed > budget:
            self.report["failed_checks"].append(
                {
                    "check": f"{check} timing budget",
                    "observed": f"{elapsed:.3f}s exceeds {budget:.3f}s",
                }
            )
        if check in self:
            self.report["failed_checks"].append(
                {
                    "check": f"{check} completed once",
                    "observed": "Duplicate check cannot substitute for a missing journey",
                }
            )
        super().append(check)

    def finish(self):
        for check in self.budgets.keys() - set(self):
            self.report["failed_checks"].append(
                {
                    "check": f"{check} completed",
                    "observed": "Required journey did not complete",
                }
            )


def expect_network_diagnostic(report, seat, method, url, *, status=None, failure=None):
    """Declare one exact deliberate refusal or injected network failure.

    A matching observed response/failure and a browser resource URL are both
    required. JavaScript console.error calls from an app asset never match.
    Every declaration accounts for at most one browser diagnostic.
    """
    if (status is None) == (failure is None):
        raise ValueError("Declare exactly one response status or network failure")
    report["expected_network_diagnostics"].append(
        {
            "seat": seat,
            "method": method,
            "url": url,
            "step": len(report["checks"]),
            **({"status": status} if status is not None else {"failure": failure}),
        }
    )


def declare_refused_poll(report, seat, method, url, status, refused_urls):
    """Declare a sheet-history poll refused for a character the seat just lost.

    Only a GET answered 404 at one of this seat's refused URLs is declared.
    Any other response stays undeclared, so its console error still fails
    the report. Returns whether a declaration was made.
    """
    if method == "GET" and status == 404 and url in refused_urls:
        expect_network_diagnostic(report, seat, method, url, status=404)
        return True
    return False


def record_console(report, text, location, seat, *, argument_count=None):
    report["console_errors"].append(text)
    report["console_events"].append(
        {
            "text": text,
            "location": location,
            "seat": seat,
            "step": len(report["checks"]),
            "argument_count": argument_count,
        }
    )


def _diagnostic_matches(event, rule, report):
    if (
        event.get("argument_count") != 0
        or event["seat"] != rule["seat"]
        or event["location"].get("url") != rule["url"]
        or event["step"] != rule["step"]
    ):
        return False
    if "status" in rule:
        expected = rf"Failed to load resource: the server responded with a status of {rule['status']}(?: \([^\n]+\))?"
        observations = report["responses"]
    else:
        expected = re.escape(f"Failed to load resource: {rule['failure']}")
        observations = report["network_failures"]
    return bool(re.fullmatch(expected, event["text"])) and any(
        all(
            observation.get(key) == value
            for key, value in rule.items()
            if key != "step"
        )
        for observation in observations
    )


def finish_report(report):
    if report.get("finalized"):
        return
    if isinstance(report["checks"], CheckRecorder):
        report["checks"].finish()
    remaining = list(report["expected_network_diagnostics"])
    unexpected = []
    for event in report["console_events"]:
        matched = next(
            (rule for rule in remaining if _diagnostic_matches(event, rule, report)),
            None,
        )
        if matched is None:
            unexpected.append(event["text"])
        else:
            remaining.remove(matched)
            report["expected_console_diagnostics"].append(event | {"expected": matched})
    # Legacy callers may supply only plain console_errors; never discard them.
    if report["console_events"]:
        report["console_errors"] = unexpected
    budget = report.get("request_budget_seconds")
    report["slow_requests"] = [
        request
        for request in report["requests"]
        if budget is not None
        and request.get("seconds", 0) > budget
        and not request.get("injected")
    ]
    report["passed"] = bool(
        report.get("passed", True)
        and not any(
            report.get(key)
            for key in (
                "failed_checks",
                "errors",
                "console_errors",
                "slow_requests",
                "capture_errors",
                "failure",
            )
        )
    )
    report["finalized"] = True


def aggregate_reports(cells):
    required = json.loads(Path(__file__).with_name("budgets.json").read_text())
    result = {
        "simulated": True,
        "mode": "all",
        "matrix": [],
        "failed_checks": [],
        "unavailable": [],
    }
    observed = set()
    for cell in cells:
        key = (cell["mode"], cell["width"])
        label = f"{key[0]} at {key[1]}px"
        observed.add(key)
        path = Path(cell["directory"]) / "report.json"
        try:
            child = json.loads(path.read_text())
            if not isinstance(child, dict):
                raise ValueError("Report must be an object")
            if (child.get("mode"), child.get("width")) != key:
                raise ValueError("Report belongs to another matrix cell")
            if key not in MATRIX:
                raise ValueError("Unexpected matrix cell")
            checks = child.get("checks", [])
            if (
                not isinstance(checks, list)
                or any(not isinstance(check, str) for check in checks)
                or len(checks) != len(required[key[0]])
                or set(checks) != set(required[key[0]])
            ):
                raise ValueError(
                    "Required journey inventory is incomplete or duplicated"
                )
            failed = (
                cell["exit_code"] != 0
                or child.get("passed") is not True
                or any(
                    child.get(field)
                    for field in (
                        "failed_checks",
                        "errors",
                        "console_errors",
                        "slow_requests",
                        "capture_errors",
                        "failure",
                    )
                )
            )
            result["matrix"].append(
                cell
                | {
                    "passed": not failed,
                    "checks": len(child.get("checks", [])),
                    "report": str(path),
                }
            )
            result["unavailable"].extend(child.get("unavailable", []))
            if failed:
                result["failed_checks"].append(
                    {
                        "check": label,
                        "observed": f"Child failed (exit {cell['exit_code']}); inspect {path}",
                    }
                )
        except (OSError, ValueError, TypeError) as error:
            result["matrix"].append(cell | {"passed": False, "report": str(path)})
            result["failed_checks"].append(
                {"check": label, "observed": f"Report missing or invalid: {error}"}
            )
    for mode, width in set(MATRIX) - observed:
        result["failed_checks"].append(
            {
                "check": f"{mode} at {width}px",
                "observed": "Required matrix cell missing",
            }
        )
    if len(cells) != len(observed) or observed - set(MATRIX):
        result["failed_checks"].append(
            {
                "check": "Exact viewport matrix",
                "observed": "Duplicate or unexpected matrix cell",
            }
        )
    result["passed"] = not result["failed_checks"]
    return result
