"""Synthetic consistency tests. These fixtures are never operational evidence."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import pytest

from scripts import stop_acceptance as check


@pytest.fixture
def evidence():
    rows = []

    def at(second):
        return f"2026-11-12T00:00:{second:02d}+00:00"

    def add(kind, second, runtime=None, seq=None, **values):
        row = dict(
            id=f"{kind}-{runtime}-{seq}",
            kind=kind,
            at=at(second),
            source="live_export",
            raw=[dict(path="raw.json", sha256="synthetic", locator="fixture-only")],
        )
        if runtime:
            row["runtime"] = runtime
        if seq:
            row.update(
                session_id=runtime,
                guest_id="guest-" + runtime,
                turn_seq=seq,
                dispatch_id=f"{runtime}-{seq}",
                claim_owner="owner",
                dispatch_count=1,
            )
        row.update(values)
        rows.append(row)
        return row

    deployed = dict(revision="a" * 40, chart_version="1.0.0", verified=True)
    add(
        "deployment",
        0,
        monolith=deployed,
        embervm=deployed,
        contains_prs=[6709, 6758, 6755],
        stop_configuration="No historical flag; exact-turn path present",
    )
    for runtime in check.RUNTIMES:
        for seq, offset in ((1, 0), (2, 20)):
            add("dispatch", offset + 1, runtime, seq)
            usage = [
                dict(
                    id=f"usage-{runtime}-{seq}",
                    dispatch_id=f"{runtime}-{seq}",
                    observed=True,
                    units={"input_tokens": seq, "output_tokens": 1},
                    list_cost_usd="0.25",
                )
            ]
            add(
                "result",
                offset + 10,
                runtime,
                seq,
                terminal_reason="user_interrupt" if seq == 1 else "completed",
                vm_destroyed=False,
                usage=usage,
                list_cost_usd="0.25",
            )
            add(
                "receipt",
                offset + 11,
                runtime,
                seq,
                authoritative=True,
                result_id=f"result-{runtime}-{seq}",
                usage_ids=[usage[0]["id"]],
                list_cost_usd="0.25",
            )
            add(
                "settlement",
                offset + 12,
                runtime,
                seq,
                state="settled",
                receipt_id=f"receipt-{runtime}-{seq}",
                usage_ids=[usage[0]["id"]],
                list_cost_usd="0.25",
            )
            add(
                "ledger",
                offset + 13,
                runtime,
                seq,
                postings=[
                    dict(
                        usage_id=usage[0]["id"],
                        receipt_id=f"receipt-{runtime}-{seq}",
                        settlement_id=f"settlement-{runtime}-{seq}",
                        list_cost_usd="0.25",
                    )
                ],
                balance_before_usd="0.00" if seq == 1 else "0.25",
                balance_after_usd="0.25" if seq == 1 else "0.50",
            )
        add(
            "export_inventory",
            39,
            runtime,
            complete=True,
            turns=[1, 2],
            record_ids=[r["id"] for r in rows if r.get("runtime") == runtime],
        )
        for kind, status, second in (
            ("duplicate", 202, 15),
            ("stale", 409, 35),
            ("non_owner", 403, 36),
        ):
            add(
                kind,
                second,
                runtime,
                1,
                http_status=status,
                additional_relays=0,
                additional_settlements=0,
                additional_postings=0,
                additional_destroys=0,
            )
    add(
        "background_notification",
        22,
        "claude",
        1,
        ignored_by_dispatch_id="claude-2",
        additional_postings=0,
        successor_terminated=False,
    )
    for outcome, second, seq in (
        ("requested", 4, 1),
        ("confirmed", 14, 1),
        ("completed", 34, 2),
        ("failed_unknown", 5, 1),
        ("completion_wins", 34, 2),
    ):
        add(
            "browser_" + outcome,
            second,
            "claude",
            seq,
            capture="actual_browser",
            trace="raw.json",
            screenshot="raw.json",
            status_dispatch_id=f"claude-{seq}",
            ui_outcome={
                "failed_unknown": "unknown",
                "completion_wins": "completed",
            }.get(outcome, outcome),
            stop_requested_at=at(2 if seq == 1 else 23),
        )
    add(
        "delayed_stop",
        8,
        "codex",
        1,
        requested_at=at(2),
        queued_at=at(3),
        successor_dispatch_id="codex-2",
        successor_admitted=False,
        reservation_held=True,
        capacity_held=True,
        held_until=at(10),
    )
    add(
        "destroy_fallback",
        50,
        session_id="isolated-fault",
        guest_id="fault-guest",
        turn_seq=1,
        dispatch_id="fault-dispatch",
        claim_owner="owner",
        dispatch_count=1,
        failure="timeout",
        fault_started_at=at(40),
        stop_requested_at=at(41),
        stop_failed_at=at(42),
        destroy_requested_at=at(43),
        destroy_confirmed_at=at(44),
        cleaned_at=at(45),
        scope="session",
        cleaned=True,
        unrelated_sessions_healthy=True,
        control_session_ids=["unaffected-session"],
        destroy_terminal=True,
    )
    return dict(
        schema_version=1, source="live_export", started_at=at(0), ended_at=at(59)
    ), rows


def assess(evidence):
    return check.assess(*evidence, now=datetime(2026, 11, 12, 1, tzinfo=timezone.utc))


def row(evidence, kind, runtime="claude", seq=1):
    return next(
        r
        for r in evidence[1]
        if r["kind"] == kind
        and r.get("runtime") == runtime
        and r.get("turn_seq") == seq
    )


def test_consistent_fixture_still_requires_raw_review(evidence):
    assert assess(evidence)["operational_acceptance"] == "pending_raw_evidence_review"


@pytest.mark.parametrize(
    "kind,key,value",
    [
        ("result", "usage", []),
        ("result", "list_cost_usd", "NaN"),
        ("result", "vm_destroyed", True),
        ("receipt", "authoritative", False),
        ("settlement", "state", "held"),
        ("ledger", "balance_after_usd", "0.50"),
        ("duplicate", "http_status", 409),
        ("duplicate", "additional_relays", 1),
        ("stale", "additional_postings", 1),
        ("non_owner", "http_status", 202),
        ("browser_requested", "capture", "mock"),
        ("browser_confirmed", "dispatch_id", "stale"),
    ],
)
def test_rejects_unproven_or_conflicting_evidence(evidence, kind, key, value):
    row(evidence, kind)[key] = value
    with pytest.raises(check.Incomplete):
        assess(evidence)


def test_duplicate_ledger_posting_rejected(evidence):
    ledger = row(evidence, "ledger")
    ledger["postings"].append(deepcopy(ledger["postings"][0]))
    with pytest.raises(check.Incomplete, match="postings"):
        assess(evidence)


def test_successor_cannot_rebill_interrupted_usage(evidence):
    first = row(evidence, "result")["usage"][0]
    row(evidence, "result", seq=2)["usage"].append(first)
    with pytest.raises(check.Incomplete, match="more than once"):
        assess(evidence)


@pytest.mark.parametrize(
    "kind,second",
    [
        ("settlement", "09"),
        ("duplicate", "25"),
        ("browser_requested", "11"),
        ("browser_confirmed", "09"),
    ],
)
def test_temporal_contradictions(evidence, kind, second):
    row(evidence, kind)["at"] = f"2026-11-12T00:00:{second}+00:00"
    with pytest.raises(check.Incomplete):
        assess(evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("successor_admitted", True),
        ("reservation_held", False),
        ("capacity_held", False),
        ("held_until", "2026-11-12T00:00:09+00:00"),
    ],
)
def test_requested_only_window(evidence, field, value):
    row(evidence, "delayed_stop", "codex")[field] = value
    with pytest.raises(check.Incomplete):
        assess(evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "global"),
        ("cleaned", False),
        ("control_session_ids", []),
        ("destroy_terminal", False),
        ("destroy_confirmed_at", "2026-11-12T00:00:40+00:00"),
    ],
)
def test_fault_fallback_incomplete(evidence, field, value):
    fault = next(r for r in evidence[1] if r["kind"] == "destroy_fallback")
    fault[field] = value
    with pytest.raises(check.Incomplete):
        assess(evidence)


def test_missing_case_and_stale_bundle(evidence):
    evidence[1].pop()
    with pytest.raises(check.Incomplete, match="destroy_fallback"):
        assess(evidence)
    evidence[0]["started_at"] = "2026-11-01T00:00:00+00:00"
    with pytest.raises(check.Incomplete, match="stale"):
        assess(evidence)


def test_old_notification_cannot_affect_successor(evidence):
    row(evidence, "background_notification")["successor_terminated"] = True
    with pytest.raises(check.Incomplete, match="notification"):
        assess(evidence)


@pytest.mark.parametrize("value", [None, "unknown", "-1", "Infinity", 0.25])
def test_invalid_cost(value):
    with pytest.raises(check.Incomplete):
        check.money(value)


@pytest.mark.parametrize("value", [None, "bad", "2026-11-12T00:00:00"])
def test_invalid_timestamp(value):
    with pytest.raises(check.Incomplete):
        check.stamp(value)


def test_cli_rejects_fixture_and_duplicate_json(tmp_path, capsys):
    path = tmp_path / "bundle.json"
    path.write_text('{"schema_version":1,"source":"fixture"}')
    assert check.main([str(path)]) == 1
    assert "fixtures" in capsys.readouterr().out
    path.write_text('{"source":"live_export","source":"fixture"}')
    assert check.main([str(path)]) == 1
    assert "duplicate JSON key" in capsys.readouterr().out


def test_file_hashes_and_path_escape(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text("{}")
    record = tmp_path / "record.json"
    ref = dict(
        path="raw.json",
        sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
        locator="audit:123",
    )
    record.write_text(json.dumps(dict(source="live_export", raw=[ref])))
    bundle = dict(
        schema_version=1,
        source="live_export",
        files=[
            dict(
                path="record.json",
                sha256=hashlib.sha256(record.read_bytes()).hexdigest(),
            )
        ],
    )
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    assert len(check.load_bundle(path)[1]) == 1
    raw.write_text('{"changed":true}')
    with pytest.raises(check.Incomplete, match="hash mismatch"):
        check.load_bundle(path)
    bundle["files"][0]["path"] = "../outside.json"
    path.write_text(json.dumps(bundle))
    with pytest.raises(check.Incomplete, match="invalid evidence path"):
        check.load_bundle(path)


@pytest.mark.parametrize("reason", ["end_turn", "stop"])
def test_native_success_reasons_preserved(evidence, reason):
    row(evidence, "result", seq=2)["terminal_reason"] = reason
    assert assess(evidence)["status"] == "consistent"


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        '{"schema_version": NaN}',
        "{bad",
        '{"schema_version":1,"source":"live_export","files":null}',
    ],
)
def test_malformed_bundle_fails_closed(tmp_path, capsys, body):
    path = tmp_path / "bundle.json"
    path.write_text(body)
    assert check.main([str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"


def test_raw_path_escape(tmp_path):
    record = tmp_path / "record.json"
    record.write_text(
        json.dumps(
            dict(
                source="live_export",
                raw=[dict(path="../outside", sha256="irrelevant", locator="row")],
            )
        )
    )
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps(
            dict(
                schema_version=1,
                source="live_export",
                files=[
                    dict(
                        path="record.json",
                        sha256=hashlib.sha256(record.read_bytes()).hexdigest(),
                    )
                ],
            )
        )
    )
    with pytest.raises(check.Incomplete, match="raw reference escapes"):
        check.load_bundle(bundle)


def test_extra_accounting_row_rejected(evidence):
    extra = deepcopy(row(evidence, "ledger"))
    extra["id"] = "unexpected-extra-charge"
    extra["turn_seq"] = 3
    evidence[1].append(extra)
    with pytest.raises(check.Incomplete, match="inventory"):
        assess(evidence)


def test_usage_allocation_cost_rejected(evidence):
    row(evidence, "ledger")["postings"][0]["list_cost_usd"] = "0.50"
    with pytest.raises(check.Incomplete, match="charge mismatch"):
        assess(evidence)


def test_unknown_usage_rejected(evidence):
    row(evidence, "result")["usage"][0]["observed"] = False
    with pytest.raises(check.Incomplete, match="unknown"):
        assess(evidence)
