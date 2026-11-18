"""Require twenty consecutive complete phone sessions in the CI environment."""

import argparse
import json
import subprocess
import sys
from html import escape
from importlib.metadata import distribution
from pathlib import Path

from rehearsal_report import print_failure_summary, report_failures

RUNS = 20
HERE = Path(__file__).resolve().parent


def write_summary(output, summary):
    """Expose the same verification results in JSON and portable Markdown."""
    (output / "report.json").write_text(json.dumps(summary, indent=2) + "\n")
    source = summary["source"]
    runs = summary["runs"]
    passed = sum(run["passed"] for run in runs)
    failed = any(not run["passed"] for run in runs)
    verdict = "Passed" if summary["passed"] else "Failed" if failed else "Incomplete"
    lines = [
        "# Grimoire 390px session stability",
        "",
        f"**{verdict}: {passed}/{summary['required_runs']} consecutive sessions passed.**",
        "",
        f"Source SHA: <code>{escape(source['sha'])}</code>.",
        f"Playwright: <code>{escape(source['playwright'])}</code>.",
        f"Chromium: <code>{escape(source['browser'])}</code>.",
        "",
        "Each passing attempt verified the complete required session inventory,",
        "the clean source SHA and pinned browser, including these HOME-223 checks:",
        "",
        "- Concurrent sheet edits merge without loss and stale saves are rejected",
        "- Repeated Enter while campaign creation is pending creates exactly one campaign",
        "",
        "No failed attempt is retried. The artifact contains every child report,",
        "launcher log and browser trace under `stability/<attempt>/`.",
        "",
        "| Attempt | Result | Child report in artifact |",
        "| --- | --- | --- |",
    ]
    for run in runs:
        result = "Passed" if run["passed"] else "Failed"
        lines.append(f"| {run['attempt']} | {result} | <code>{run['report']}</code> |")
        for failure in run["failed_checks"]:
            error = next(
                (
                    line
                    for line in str(failure["observed"]).splitlines()
                    if line.strip()
                ),
                "No error detail",
            )
            # Keep assertion text from becoming HTML or additional table cells.
            detail = escape(f"{failure['check']}: {error}").replace("|", "&#124;")
            lines.append(f"| | <code>{detail}</code> | |")
    (output / "report.md").write_text("\n".join(lines) + "\n")


def check_run(directory, exit_code, source):
    """A process exit alone cannot certify a complete, current-browser journey."""
    failures = []
    if exit_code:
        failures.append({"check": "Launcher", "observed": f"Exit {exit_code}"})
    try:
        report = json.loads((directory / "report.json").read_text())
        failures.extend(report_failures(report))
        required = json.loads((HERE / "budgets.json").read_text())["session"]
        checks = report.get("checks", [])
        if len(checks) != len(required) or set(checks) != set(required):
            raise ValueError(
                "Required session check inventory is incomplete or duplicated"
            )
        if report.get("mode") != "session" or report.get("width") != 390:
            raise ValueError("Expected the complete session journey at 390px")
        if report.get("source") != source:
            raise ValueError(
                "Source SHA, clean checkout or pinned browser does not match"
            )
        if report.get("passed") is not True:
            raise ValueError("Session report did not pass")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        failures.append({"check": "Session evidence", "observed": str(error)})
    return failures


def rehearse(directory):
    # Use exactly the launcher's ordinary CI session path: same interpreter,
    # built bundle and browser, with a new disposable database on each launch.
    with (directory / "launcher.log").open("w") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                str(HERE / "run.py"),
                "--rehearse",
                "--viewport",
                "390",
                "--output",
                str(directory),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            return child.wait(timeout=960)
        except subprocess.TimeoutExpired:
            # Let run.py stop PostgreSQL before escalating to a kill.
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            return 124


def run_stability(output, source):
    output.mkdir(parents=True, exist_ok=True)
    summary = {"source": source, "required_runs": RUNS, "runs": [], "passed": False}
    # A restarted validation must not expose a previous successful summary.
    write_summary(output, summary)
    for attempt in range(1, RUNS + 1):
        directory = output / str(attempt)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.json").unlink(missing_ok=True)
        failures = check_run(directory, rehearse(directory), source)
        summary["runs"].append(
            {
                "attempt": attempt,
                "passed": not failures,
                "report": f"{attempt}/report.json",
                "failed_checks": failures,
            }
        )
        summary["passed"] = attempt == RUNS and not failures
        write_summary(output, summary)
        print(
            f"Stability session 390px {attempt}/{RUNS}: "
            f"{'failed' if failures else 'passed'}; {directory}",
            flush=True,
        )
        print_failure_summary({"failed_checks": failures})
        if failures:
            # No retries or discarded failures can create a passing streak.
            break
    return summary["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package = distribution("playwright")
    browsers = json.loads(
        package.locate_file("playwright/driver/package/browsers.json").read_text()
    )["browsers"]
    source = {
        "sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "dirty": False,
        "playwright": package.version,
        "browser": next(
            browser["browserVersion"]
            for browser in browsers
            if browser["name"] == "chromium-headless-shell"
        ),
    }
    if not run_stability(args.output, source):
        raise SystemExit(
            "Grimoire stability gate failed; inspect stability/report.json"
        )


if __name__ == "__main__":
    main()
