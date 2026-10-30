"""Manual open-issue gate cleanup. Dry-run by default; no scheduler entry."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from factory.orchestration.factory_gate_blocks import blocks, rewrite

REPO = "jomcgi-org/homelab"


def gh_api(suffix: str, payload: dict | None = None):
    args = ["gh", "api", f"repos/{REPO}/{suffix}"]
    if payload is not None:
        args += ["--method", "PATCH", "--input", "-"]
    result = subprocess.run(
        args,
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        check=True,
        timeout=45,
    )
    return json.loads(result.stdout)


def open_issues(api):
    page = 1
    seen = set()
    while True:
        rows = api(f"issues?state=open&per_page=100&page={page}")
        if not isinstance(rows, list):
            raise TypeError("issue enumeration returned a non-list")
        for row in rows:
            if row["number"] in seen:
                raise ValueError(
                    "issue enumeration repeated an issue; coverage unknown"
                )
            seen.add(row["number"])
            if row.get("state") == "open" and "pull_request" not in row:
                yield row
        if len(rows) < 100:
            return
        page += 1


def cleanup(issues, api, emit, *, apply=False, audit=False):
    """Report each case and coverage; uncertain writes stop this invocation."""
    errors = duplicates = changed = 0
    try:
        for issue in issues:
            number = issue["number"]
            if issue.get("state") != "open" or "pull_request" in issue:
                emit(
                    {
                        "number": number,
                        "status": "skipped",
                        "reason": "not an open issue",
                    }
                )
                continue
            before = issue.get("body") or ""
            try:
                found = blocks(before)
                # Unmarked rescope lines could be manual or malformed. Report
                # them for inspection; they cannot become a passing audit.
                unowned = [
                    match.start()
                    for match in re.finditer(r"(?m)^Conductor rescope:", before)
                    if not any(
                        block.start <= match.start() < block.end for block in found
                    )
                ]
                if unowned:
                    raise ValueError("unowned rescope lines require manual inspection")
                duplicates += len(found) > 1
                if audit or len(found) < 2:
                    emit(
                        {
                            "number": number,
                            "status": "duplicate" if len(found) > 1 else "skipped",
                            "blocks": len(found),
                        }
                    )
                    continue
                after, _ = rewrite(before)
            except ValueError as exc:
                errors += 1
                emit({"number": number, "status": "error", "reason": str(exc)})
                continue
            record = {
                "number": number,
                "status": "would_change",
                "blocks": len(found),
                "before": before,
                "after": after,
                "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
            }
            if apply:
                fresh = api(f"issues/{number}")
                if (
                    fresh.get("state") != "open"
                    or "pull_request" in fresh
                    or (fresh.get("body") or "") != before
                    or not issue.get("updated_at")
                    or fresh.get("updated_at") != issue["updated_at"]
                ):
                    errors += 1
                    emit(
                        {
                            "number": number,
                            "status": "skipped",
                            "reason": "stale or closed issue",
                        }
                    )
                    continue
                # Emit intent before entering the write. A timeout/error may
                # follow a successful write; stop and reconcile, never retry.
                emit({**record, "status": "write_started"})
                response = api(f"issues/{number}", {"body": after})
                if response.get("body") != after:
                    raise ValueError("write response did not confirm expected body")
                record["status"] = "changed"
            changed += 1
            emit(record)
    except Exception as exc:  # noqa: BLE001 -- report and stop any uncertain write
        errors += 1
        emit(
            {
                "status": "error",
                "reason": str(exc),
                "coverage": "incomplete or write outcome unknown; inspect before rerun",
            }
        )
    emit(
        {
            "status": "summary",
            "errors": errors,
            "duplicates": duplicates,
            "changed": changed,
            "apply": apply,
            "audit": audit,
        }
    )
    return 1 if errors or (audit and duplicates) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--audit", action="store_true")
    parser.add_argument(
        "--fixture", type=Path, help="offline issue JSON array; never writes"
    )
    args = parser.parse_args()
    if args.fixture and args.apply:
        parser.error("--fixture cannot be combined with --apply")
    issues = (
        json.loads(args.fixture.read_text()) if args.fixture else open_issues(gh_api)
    )
    return cleanup(
        issues,
        gh_api,
        lambda row: print(json.dumps(row), flush=True),
        apply=args.apply,
        audit=args.audit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
