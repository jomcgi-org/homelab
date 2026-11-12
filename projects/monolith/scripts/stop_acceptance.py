"""Offline consistency checks for exported #6564 evidence. Never drives a session."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

RUNTIMES = ("claude", "codex", "muse", "pi")
SUCCESS_REASONS = {"completed", "end_turn", "stop"}
IDENTITY = (
    "session_id",
    "guest_id",
    "turn_seq",
    "dispatch_id",
    "claim_owner",
    "dispatch_count",
)


class Incomplete(ValueError):
    """Evidence cannot establish acceptance."""


def require(condition, message):
    if not condition:
        raise Incomplete(message)


def stamp(value):
    require(isinstance(value, str), "missing timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Incomplete("invalid timestamp") from exc
    require(parsed.tzinfo is not None, "timestamp needs timezone")
    return parsed


def money(value):
    require(isinstance(value, str), "cost must be an observed decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise Incomplete("invalid cost") from exc
    require(parsed.is_finite() and parsed >= 0, "invalid cost")
    return parsed


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    def invalid_constant(value):
        raise Incomplete(f"non-JSON number: {value}")

    value = json.loads(
        path.read_text(),
        object_pairs_hook=strict_object,
        parse_constant=invalid_constant,
    )
    require(isinstance(value, dict), "JSON document must be an object")
    return value


def load_bundle(path):
    """Read only files inside the export directory and verify their hashes."""
    root = path.resolve().parent
    bundle = read_json(path)
    require(bundle.get("schema_version") == 1, "unsupported schema_version")
    require(bundle.get("source") == "live_export", "fixtures are never live evidence")
    require(
        isinstance(bundle.get("files"), list) and bool(bundle["files"]),
        "missing evidence files",
    )
    records = []
    for item in bundle["files"]:
        local = (root / item["path"]).resolve()
        require(
            local.is_relative_to(root) and local != path.resolve(),
            "invalid evidence path",
        )
        require(
            hashlib.sha256(local.read_bytes()).hexdigest() == item["sha256"],
            "raw file hash mismatch",
        )
        # Each file is an operator-normalized record with references to raw exports.
        record = read_json(local)
        for ref in record["raw"]:
            raw = (root / ref["path"]).resolve()
            require(raw.is_relative_to(root), "raw reference escapes export")
            require(bool(ref["locator"]), "raw record locator missing")
            require(
                hashlib.sha256(raw.read_bytes()).hexdigest() == ref["sha256"],
                "raw reference hash mismatch",
            )
        require(bool(record["raw"]), "raw references missing")
        require(record.get("source") == "live_export", "synthetic record")
        records.append(record)
    return bundle, records


def assess(bundle, records, now=None):
    """Check normalized records; authenticity and export completeness need owner review."""
    now = now or datetime.now(timezone.utc)
    start, end = stamp(bundle["started_at"]), stamp(bundle["ended_at"])
    require(
        start <= end <= now and now - start <= timedelta(hours=72),
        "stale or future evidence window",
    )
    require(bundle.get("source") == "live_export", "synthetic bundle")
    ids = set()
    turn_kinds = {
        "dispatch",
        "result",
        "receipt",
        "settlement",
        "ledger",
        "duplicate",
        "stale",
        "non_owner",
        "background_notification",
        "delayed_stop",
        "browser_requested",
        "browser_confirmed",
        "browser_completed",
        "browser_failed_unknown",
        "browser_completion_wins",
    }
    for record in records:
        require(
            record["kind"]
            in turn_kinds | {"deployment", "export_inventory", "destroy_fallback"},
            "unknown evidence kind",
        )
        if record["kind"] in turn_kinds | {"export_inventory"}:
            require(record.get("runtime") in RUNTIMES, "missing or unknown runtime")
        require(record["id"] not in ids, "duplicate record id")
        ids.add(record["id"])
        require(start <= stamp(record["at"]) <= end, "record outside evidence window")
        require(record.get("source") == "live_export", "synthetic record")
        require(record.get("raw"), "record lacks raw references")

    def one(kind, runtime=None, turn=None):
        matches = [
            r
            for r in records
            if r["kind"] == kind
            and (runtime is None or r.get("runtime") == runtime)
            and (turn is None or r.get("turn_seq") == turn)
        ]
        require(
            len(matches) == 1, f"need one {kind}/{runtime}/{turn}; got {len(matches)}"
        )
        return matches[0]

    def same(left, right):
        require(
            all(
                left.get(k) is not None and left.get(k) == right.get(k)
                for k in IDENTITY
            ),
            "misattributed dispatch evidence",
        )

    def ordered(*rows):
        times = [stamp(r["at"]) for r in rows]
        require(
            all(a < b for a, b in zip(times, times[1:])),
            "contradictory timestamp order",
        )

    deployment = one("deployment")
    require(stamp(deployment["at"]) <= start, "deployment must precede run")
    for service in ("monolith", "embervm"):
        deployed = deployment[service]
        require(
            re.fullmatch(r"[0-9a-f]{40}", deployed["revision"]) is not None,
            "missing deployed revision",
        )
        require(
            deployed["verified"] is True and bool(deployed["chart_version"]),
            "unverified deployment",
        )
    require(
        set(deployment["contains_prs"]) >= {6709, 6758, 6755}, "fix ancestry missing"
    )
    require(bool(deployment["stop_configuration"]), "actual Stop configuration missing")

    turns = {}
    all_usage = set()
    for runtime in RUNTIMES:
        current = []
        for seq in (1, 2):
            dispatch, result, receipt, settlement, ledger = [
                one(k, runtime, seq)
                for k in ("dispatch", "result", "receipt", "settlement", "ledger")
            ]
            for row in (result, receipt, settlement, ledger):
                same(dispatch, row)
            require(
                type(dispatch["dispatch_count"]) is int
                and dispatch["dispatch_count"] > 0,
                "invalid dispatch count",
            )
            require(
                all(bool(dispatch[k]) for k in IDENTITY), "missing dispatch identity"
            )
            ordered(dispatch, result, settlement)
            require(
                stamp(result["at"]) <= stamp(receipt["at"]) <= stamp(settlement["at"]),
                "receipt order",
            )
            require(
                stamp(settlement["at"]) <= stamp(ledger["at"]),
                "ledger precedes settlement",
            )
            require(
                result["terminal_reason"]
                in ({"user_interrupt"} if seq == 1 else SUCCESS_REASONS),
                "unexpected terminal reason",
            )
            require(result["vm_destroyed"] is False, "Stop destroyed VM")
            require(
                receipt["authoritative"] is True
                and receipt["result_id"] == result["id"],
                "receipt not authoritative",
            )
            require(
                settlement["receipt_id"] == receipt["id"]
                and settlement["state"] == "settled",
                "unsettled reservation",
            )
            # Export every usage component and every ledger posting, including reversals.
            # Disjoint component IDs and exact allocations prevent cumulative successor rebilling.
            components = result["usage"]
            require(bool(components), "missing/unknown observed usage")
            usage_ids = [c["id"] for c in components]
            require(
                len(set(usage_ids)) == len(usage_ids)
                and not all_usage.intersection(usage_ids),
                "usage billed across turns more than once",
            )
            all_usage.update(usage_ids)
            for component in components:
                require(
                    component["observed"] is True
                    and component["dispatch_id"] == dispatch["dispatch_id"],
                    "unknown or misattributed usage",
                )
                require(
                    isinstance(component["units"], dict)
                    and bool(component["units"])
                    and all(
                        type(v) is int and v >= 0 for v in component["units"].values()
                    ),
                    "unknown usage units",
                )
            total = sum((money(c["list_cost_usd"]) for c in components), Decimal(0))
            require(money(result["list_cost_usd"]) == total, "usage cost mismatch")
            require(
                receipt["usage_ids"] == usage_ids
                and money(receipt["list_cost_usd"]) == total,
                "receipt cost mismatch",
            )
            require(
                settlement["usage_ids"] == usage_ids
                and money(settlement["list_cost_usd"]) == total,
                "settlement cost mismatch",
            )
            postings = ledger["postings"]
            require(
                len(postings) == len(components), "duplicate or missing ledger postings"
            )
            require(
                {p["usage_id"] for p in postings} == set(usage_ids),
                "ledger allocation mismatch",
            )
            expected = {c["id"]: money(c["list_cost_usd"]) for c in components}
            for posting in postings:
                require(
                    posting["receipt_id"] == receipt["id"]
                    and posting["settlement_id"] == settlement["id"]
                    and money(posting["list_cost_usd"])
                    == expected[posting["usage_id"]],
                    "ledger charge mismatch",
                )
            require(
                money(ledger["balance_after_usd"]) - money(ledger["balance_before_usd"])
                == total,
                "ledger delta includes extra billing",
            )
            current.append((dispatch, result, settlement, ledger))
        first, successor = current
        require(
            first[0]["session_id"] == successor[0]["session_id"]
            and first[0]["guest_id"] == successor[0]["guest_id"],
            "successor did not reuse VM",
        )
        require(
            first[0]["dispatch_id"] != successor[0]["dispatch_id"],
            "successor dispatch reused",
        )
        ordered(first[2], successor[0])
        require(
            money(first[3]["balance_after_usd"])
            == money(successor[3]["balance_before_usd"]),
            "unreconciled between-turn billing",
        )
        audit = one("export_inventory", runtime)
        require(
            audit["complete"] is True and set(audit["turns"]) == {1, 2},
            "partial accounting export",
        )
        require(len(audit["record_ids"]) == 10, "extra or missing accounting records")
        require(
            set(audit["record_ids"])
            == {
                r["id"]
                for r in records
                if r.get("runtime") == runtime
                and r["kind"]
                in {"dispatch", "result", "receipt", "settlement", "ledger"}
            },
            "accounting inventory mismatch",
        )
        for kind, status in (("duplicate", 202), ("stale", 409), ("non_owner", 403)):
            check = one(kind, runtime)
            same(first[0], check)
            require(
                check["http_status"] == status
                and check["additional_relays"] == 0
                and check["additional_settlements"] == 0
                and check["additional_postings"] == 0
                and check["additional_destroys"] == 0,
                f"{kind} side effects/status",
            )
            if kind == "duplicate":
                ordered(first[1], check, successor[0])
            else:
                ordered(successor[0], check)
        turns[runtime] = current

    require(
        len({turns[r][0][0]["session_id"] for r in RUNTIMES}) == 4,
        "runtimes must use isolated sessions",
    )
    require(
        len({turns[r][0][0]["guest_id"] for r in RUNTIMES}) == 4,
        "runtimes must use isolated guests",
    )
    old, successor = turns["claude"]
    notification = one("background_notification", "claude")
    same(old[0], notification)
    require(
        abs(stamp(notification["at"]) - stamp(successor[0]["at"]))
        <= timedelta(seconds=30),
        "notification outside successor dispatch race",
    )
    require(
        notification["ignored_by_dispatch_id"] == successor[0]["dispatch_id"]
        and notification["additional_postings"] == 0
        and notification["successor_terminated"] is False,
        "stale Claude notification affected successor",
    )

    for outcome in (
        "requested",
        "confirmed",
        "completed",
        "failed_unknown",
        "completion_wins",
    ):
        browser = one("browser_" + outcome)
        require(
            browser["capture"] == "actual_browser"
            and browser["trace"]
            and browser["screenshot"],
            "mock/API-only browser evidence",
        )
        require(
            {browser["trace"], browser["screenshot"]}
            <= {ref["path"] for ref in browser["raw"]},
            "browser capture lacks raw references",
        )
        target = one("dispatch", browser["runtime"], browser["turn_seq"])
        same(target, browser)
        require(
            browser["status_dispatch_id"] == target["dispatch_id"]
            and browser["ui_outcome"]
            == {"failed_unknown": "unknown", "completion_wins": "completed"}.get(
                outcome, outcome
            ),
            "browser/status attribution mismatch",
        )
        require(
            stamp(target["at"]) <= stamp(browser["at"]), "browser precedes dispatch"
        )
        if outcome in {"confirmed", "completed", "completion_wins"}:
            result = one("result", browser["runtime"], browser["turn_seq"])
            require(
                stamp(result["at"]) <= stamp(browser["at"]),
                "UI terminal without result",
            )
            require(
                result["terminal_reason"]
                in ({"user_interrupt"} if outcome == "confirmed" else SUCCESS_REASONS),
                "browser terminal contradiction",
            )
        if outcome in {"requested", "completion_wins", "failed_unknown"}:
            require(
                browser["stop_requested_at"] is not None
                and stamp(target["at"])
                < stamp(browser["stop_requested_at"])
                <= stamp(browser["at"]),
                "missing browser Stop request order",
            )
        if outcome == "requested":
            require(
                stamp(browser["at"])
                < stamp(one("result", browser["runtime"], browser["turn_seq"])["at"]),
                "requested UI after terminal",
            )
        if outcome == "completion_wins":
            require(
                stamp(browser["stop_requested_at"]) < stamp(result["at"]),
                "completion race not exercised",
            )

    delayed = one("delayed_stop")
    first, successor = turns[delayed["runtime"]]
    same(first[0], delayed)
    ordered(first[0], delayed, first[1], first[2], successor[0])
    require(
        stamp(delayed["requested_at"])
        < stamp(delayed["queued_at"])
        < stamp(delayed["at"]),
        "no requested-only queued interval",
    )
    require(
        stamp(first[0]["at"]) < stamp(delayed["requested_at"]), "Stop precedes dispatch"
    )
    require(
        delayed["successor_dispatch_id"] == successor[0]["dispatch_id"]
        and delayed["successor_admitted"] is False
        and delayed["reservation_held"] is True
        and delayed["capacity_held"] is True,
        "requested acknowledgment released capacity",
    )
    require(
        delayed["held_until"] == first[1]["at"], "held interval lacks terminal boundary"
    )

    fault = one("destroy_fallback")
    require(
        fault["failure"] in {"timeout", "failed_relay"},
        "fallback did not exercise Stop failure",
    )
    require(
        fault["session_id"] not in {t[0][0]["session_id"] for t in turns.values()},
        "fault must use separate isolated session",
    )
    require(all(fault.get(k) for k in IDENTITY), "fault dispatch identity missing")
    times = [
        stamp(fault[k])
        for k in (
            "fault_started_at",
            "stop_requested_at",
            "stop_failed_at",
            "destroy_requested_at",
            "destroy_confirmed_at",
            "cleaned_at",
        )
    ]
    require(all(a < b for a, b in zip(times, times[1:])), "fallback order")
    require(
        start <= times[0]
        and times[-1] <= stamp(fault["at"])
        and times[-1] - times[0] <= timedelta(minutes=5),
        "unbounded fault cleanup",
    )
    require(
        fault["scope"] == "session"
        and fault["cleaned"] is True
        and fault["unrelated_sessions_healthy"] is True
        and bool(fault["control_session_ids"])
        and fault["session_id"] not in fault["control_session_ids"],
        "global fault or missing unaffected controls",
    )
    require(fault["destroy_terminal"] is True, "Destroy not confirmed")
    return {
        "status": "consistent",
        "operational_acceptance": "pending_raw_evidence_review",
        "records": len(records),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        bundle, records = load_bundle(args.bundle)
        result = assess(bundle, records)
    except (
        Incomplete,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        AttributeError,
    ) as exc:
        print(json.dumps({"status": "incomplete", "reason": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
