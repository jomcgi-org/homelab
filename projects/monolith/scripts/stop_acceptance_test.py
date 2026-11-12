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
            add("dispatch", offset + 1, runtime, seq, triggered_by="owner@example.test")
            usage = [
                dict(
                    id=f"usage-{runtime}-{seq}",
                    dispatch_id=f"{runtime}-{seq}",
                    observed=True,
                    units={"input_tokens": 1, "output_tokens": 1},
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
                cost_usd="0.25" if runtime == "claude" else None,
                native_result_id=f"native-{runtime}-{seq}",
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
                cost_usd="0.25" if runtime == "claude" else None,
                native_result_id=f"native-{runtime}-{seq}",
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
                "agent_turn",
                offset + 13,
                runtime,
                seq,
                cost_usd="0.25" if runtime == "claude" else None,
                list_cost_usd="0.25",
                usage_json=json.dumps(
                    dict(
                        **usage[0]["units"],
                        native_result_receipt=dict(
                            receipt_id=f"receipt-{runtime}-{seq}"
                        ),
                    )
                ),
            )
            add(
                "native_totals",
                offset + 10,
                runtime,
                seq,
                native_session_id=f"native-session-{runtime}",
                native_result_id=f"native-{runtime}-{seq}",
                cumulative_cost_usd=("0.25" if seq == 1 else "0.50")
                if runtime == "claude"
                else None,
                cumulative_list_cost_usd="0.25" if seq == 1 else "0.50",
                cumulative_units={"input_tokens": seq, "output_tokens": seq},
                price_per_unit_usd={"input_tokens": "0.10", "output_tokens": "0.15"},
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
            ("stale", 409, 25),
            ("non_owner", 403, 6),
        ):
            add(
                kind,
                second,
                runtime,
                1,
                http_status=status,
                additional_relays=0,
                additional_settlements=0,
                additional_billed_turns=0,
                additional_destroys=0,
                authenticated=True,
                triggered_by="owner@example.test",
                requester="someone-else@example.test",
            )
    add(
        "background_notification",
        22,
        "claude",
        1,
        native_type="result",
        origin={"kind": "task-notification"},
        native_result_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        native_session_id="native-session-claude",
        skip_log_locator="fixture-only",
        usage_ids=["notification-usage"],
        ignored_by_dispatch_id="claude-2",
        additional_billed_turns=0,
        successor_terminated=False,
    )
    for outcome, second, seq in (
        ("requested", 4, 1),
        ("confirmed", 14, 1),
        ("completed", 34, 2),
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
        hold_method={
            "name": "authorized session-local relay delay",
            "locator": "fixture-only",
        },
        history_complete=True,
        reservation_history=[
            dict(
                at=at(second),
                reservation_id="reservation-codex-1",
                reservation_held=True,
                capacity_held=True,
                successor_admitted=False,
                locator="fixture-only",
            )
            for second in (2, 8, 10)
        ],
    )
    fault = add(
        "destroy_fallback",
        50,
        runtime="claude",
        dispatched_at=at(39),
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
    fault_identity = {key: fault[key] for key in check.IDENTITY}
    backend = add(
        "backend_stop",
        42,
        "claude",
        **fault_identity,
        requested_at=at(41),
        http_status=503,
        outcome="unknown",
        terminal_result_available=False,
    )
    add(
        "browser_failed_unknown",
        43,
        "claude",
        **fault_identity,
        capture="actual_browser",
        trace="raw.json",
        screenshot="raw.json",
        status_dispatch_id="fault-dispatch",
        ui_outcome="unknown",
        stop_requested_at=at(41),
        stop_outcome_id=backend["id"],
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
        ("agent_turn", "list_cost_usd", "0.50"),
        ("duplicate", "http_status", 409),
        ("duplicate", "additional_relays", 1),
        ("stale", "additional_billed_turns", 1),
        ("non_owner", "http_status", 202),
        ("browser_requested", "capture", "mock"),
        ("browser_confirmed", "dispatch_id", "stale"),
    ],
)
def test_rejects_unproven_or_conflicting_evidence(evidence, kind, key, value):
    row(evidence, kind)[key] = value
    with pytest.raises(check.Incomplete):
        assess(evidence)


def test_duplicate_agent_turn_rejected(evidence):
    billed = deepcopy(row(evidence, "agent_turn"))
    billed["id"] = "second-billed-turn"
    evidence[1].append(billed)
    with pytest.raises(check.Incomplete, match="need one agent_turn"):
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
    with pytest.raises(check.Incomplete, match="browser_failed_unknown"):
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
    extra = deepcopy(row(evidence, "agent_turn"))
    extra["id"] = "unexpected-extra-charge"
    extra["turn_seq"] = 3
    evidence[1].append(extra)
    with pytest.raises(check.Incomplete, match="inventory"):
        assess(evidence)


def test_billed_usage_mismatch(evidence):
    row(evidence, "agent_turn")["usage_json"] = json.dumps(
        dict(
            input_tokens=2,
            output_tokens=1,
            native_result_receipt=dict(receipt_id="receipt-claude-1"),
        )
    )
    with pytest.raises(check.Incomplete, match="usage_json mismatch"):
        assess(evidence)


def test_unknown_usage_rejected(evidence):
    row(evidence, "result")["usage"][0]["observed"] = False
    with pytest.raises(check.Incomplete, match="unknown"):
        assess(evidence)


@pytest.mark.parametrize("runtime", check.RUNTIMES)
def test_cumulative_cost_cannot_be_rebilled_under_fresh_ids(evidence, runtime):
    for kind in ("result", "receipt", "settlement", "agent_turn"):
        target = row(evidence, kind, runtime, 2)
        target["list_cost_usd"] = "0.50"
        if runtime == "claude" and kind != "settlement":
            target["cost_usd"] = "0.50"
    row(evidence, "result", runtime, 2)["usage"][0]["list_cost_usd"] = "0.50"
    with pytest.raises(check.Incomplete, match="cumulative list cost"):
        assess(evidence)


@pytest.mark.parametrize("runtime", check.RUNTIMES)
def test_cumulative_native_usage_cannot_be_rebilled(evidence, runtime):
    row(evidence, "result", runtime, 2)["usage"][0]["units"] = dict(
        input_tokens=3, output_tokens=2
    )
    with pytest.raises(check.Incomplete, match="cumulative usage"):
        assess(evidence)


@pytest.mark.parametrize(
    "kind,second",
    [("stale", 21), ("stale", 30), ("stale", 35), ("non_owner", 15), ("non_owner", 36)],
)
def test_fence_and_owner_need_active_window(evidence, kind, second):
    row(evidence, kind)["at"] = f"2026-11-12T00:00:{second:02d}+00:00"
    with pytest.raises(check.Incomplete, match="timestamp order"):
        assess(evidence)


def test_non_owner_can_target_active_successor(evidence):
    probe = row(evidence, "non_owner")
    dispatch = row(evidence, "dispatch", seq=2)
    probe.update({key: dispatch[key] for key in check.IDENTITY})
    probe["at"] = "2026-11-12T00:00:25+00:00"
    assert assess(evidence)["status"] == "consistent"


@pytest.mark.parametrize(
    "field,value",
    [
        ("requester", ""),
        ("requester", " OWNER@example.test "),
        ("authenticated", False),
        ("triggered_by", "another-owner"),
    ],
)
def test_non_owner_needs_authenticated_distinct_requester(evidence, field, value):
    row(evidence, "non_owner")[field] = value
    with pytest.raises(check.Incomplete, match="authenticated distinct"):
        assess(evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("at", "2026-11-12T00:00:45+00:00"),
        ("origin", {"kind": "user"}),
        ("native_type", "assistant"),
        ("native_result_id", ""),
        ("native_session_id", "other-session"),
        ("skip_log_locator", "missing-log"),
        ("usage_ids", []),
        ("usage_ids", ["usage-claude-2"]),
    ],
)
def test_notification_requires_native_evidence(evidence, field, value):
    row(evidence, "background_notification")[field] = value
    with pytest.raises(check.Incomplete):
        assess(evidence)


def test_notification_cannot_be_successor_native_result(evidence):
    native_id = row(evidence, "background_notification")["native_result_id"]
    for kind in ("result", "receipt", "native_totals"):
        row(evidence, kind, seq=2)["native_result_id"] = native_id
    with pytest.raises(check.Incomplete, match="attributed to successor"):
        assess(evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("http_status", 202),
        ("dispatch_id", "wrong-dispatch"),
        ("requested_at", "2026-11-12T00:00:40+00:00"),
        ("outcome", "interrupted"),
        ("at", "2026-11-12T00:00:44+00:00"),
    ],
)
def test_failed_browser_needs_matching_backend_failure(evidence, field, value):
    backend = row(evidence, "backend_stop")
    backend["terminal_result_available"] = True
    backend[field] = value
    with pytest.raises(check.Incomplete):
        assess(evidence)


def test_failed_browser_rejects_successful_stop_identity(evidence):
    browser = row(evidence, "browser_failed_unknown")
    browser.update({key: row(evidence, "dispatch")[key] for key in check.IDENTITY})
    browser.update(
        status_dispatch_id="claude-1", stop_requested_at="2026-11-12T00:00:02+00:00"
    )
    with pytest.raises(check.Incomplete, match="misattributed"):
        assess(evidence)


def test_terminal_result_unavailable_is_unknown(evidence):
    backend = row(evidence, "backend_stop")
    backend.update(http_status=202, outcome="terminal_result_unavailable")
    assert assess(evidence)["status"] == "consistent"


def test_millisecond_requested_window_is_incomplete(evidence):
    delayed = row(evidence, "delayed_stop", "codex")
    delayed.update(
        requested_at="2026-11-12T00:00:09.997+00:00",
        queued_at="2026-11-12T00:00:09.998+00:00",
        at="2026-11-12T00:00:09.999+00:00",
    )
    with pytest.raises(check.Incomplete, match="hold is too short"):
        assess(evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("hold_method", {"name": "", "locator": "fixture-only"}),
        ("history_complete", False),
        ("reservation_history", []),
    ],
)
def test_delayed_stop_requires_hold_evidence(evidence, field, value):
    row(evidence, "delayed_stop", "codex")[field] = value
    with pytest.raises(check.Incomplete):
        assess(evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reservation_held", False),
        ("capacity_held", False),
        ("successor_admitted", True),
        ("locator", "missing"),
        ("reservation_id", "other"),
    ],
)
def test_mid_hold_release_cannot_be_hidden(evidence, field, value):
    row(evidence, "delayed_stop", "codex")["reservation_history"][1][field] = value
    with pytest.raises(check.Incomplete, match="during hold"):
        assess(evidence)


@pytest.mark.parametrize("kind", ["dispatch", "result", "destroy_fallback"])
def test_boolean_turn_sequence_rejected(evidence, kind):
    row(evidence, kind)["turn_seq"] = True
    with pytest.raises(check.Incomplete, match="invalid turn sequence"):
        assess(evidence)


def test_fault_guest_cannot_overlap_runtime_guest(evidence):
    for kind in ("destroy_fallback", "backend_stop", "browser_failed_unknown"):
        row(evidence, kind)["guest_id"] = "guest-claude"
    with pytest.raises(check.Incomplete, match="isolated guest"):
        assess(evidence)


@pytest.mark.parametrize("runtime", ["codex", "muse", "pi"])
def test_native_priced_total_must_follow_cumulative_tokens(evidence, runtime):
    # Even internally consistent inflated costs must match the native token totals.
    for kind in ("result", "receipt", "settlement", "agent_turn"):
        row(evidence, kind, runtime, 2)["list_cost_usd"] = "0.50"
    row(evidence, "result", runtime, 2)["usage"][0]["list_cost_usd"] = "0.50"
    row(evidence, "native_totals", runtime, 2)["cumulative_list_cost_usd"] = "0.75"
    with pytest.raises(check.Incomplete, match="token pricing"):
        assess(evidence)


def test_claude_billed_cost_reconciles_native_delta(evidence):
    for kind in ("result", "receipt", "agent_turn"):
        row(evidence, kind, seq=2)["cost_usd"] = "0.50"
    with pytest.raises(check.Incomplete, match="cumulative cost"):
        assess(evidence)


def test_agent_turn_must_link_authoritative_receipt(evidence):
    billed = row(evidence, "agent_turn")
    usage = json.loads(billed["usage_json"])
    usage["native_result_receipt"]["receipt_id"] = "unrelated"
    billed["usage_json"] = json.dumps(usage)
    with pytest.raises(check.Incomplete, match="agent_turns receipt"):
        assess(evidence)
