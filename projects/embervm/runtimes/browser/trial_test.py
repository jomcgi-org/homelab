import hashlib
import json
import struct
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from evidence import (
    DEFAULT_VIEWPORTS,
    PNG_SIGNATURE,
    EvidenceRecord,
    EvidenceVerdict,
    verify_evidence,
)
from trial import (
    ARMS,
    COUNT_FIELDS,
    RELATIVE_TOLERANCE,
    SCENARIOS,
    TOKEN_CLASSES,
    RunRecord,
    TrialSpec,
    UsageRecord,
    account_usage,
    schedule,
    select,
)

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
SPEC = TrialSpec(
    "a" * 40, "https://preview.example.test/", "b" * 64, "model", "effort", "contract"
)
PRICES = {"model": {name: 1 for name in TOKEN_CLASSES}}


def usage(request_id, phase="implementation", **changes):
    return replace(
        UsageRecord(request_id, "model", phase, 100, 10, 20, 20, 5, True), **changes
    )


def artifact(planned, data, kind, viewport=None):
    return EvidenceRecord(
        kind,
        SPEC.source_url,
        SPEC.app_commit,
        "task",
        planned.run_id,
        planned.session_id,
        "2026-09-30T23:59:00Z",
        "image/png" if kind == "screenshot" else "application/json",
        hashlib.sha256(data).hexdigest(),
        len(data),
        {"backend": "test", "uri": "artifact:test"},
        "2026-10-02T00:00:00Z",
        "https://artifacts.example.test/1",
        "uploaded",
        viewport,
    )


def record(planned):
    # Synthetic byte fixtures test the contract without a browser or provider.
    images = [
        PNG_SIGNATURE
        + b"\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", *viewport)
        + bytes(9)
        for viewport in DEFAULT_VIEWPORTS
    ]
    outcome = json.dumps(
        {
            "navigation": "passed",
            "viewports": {
                f"{w}x{h}": {
                    "functional": {"functional": "passed"},
                    "accessibility": {"accessibility": "passed"},
                }
                for w, h in DEFAULT_VIEWPORTS
            },
        }
    ).encode()
    records = tuple(
        artifact(planned, data, "screenshot", viewport)
        for data, viewport in zip(images, DEFAULT_VIEWPORTS)
    ) + (artifact(planned, outcome, "interaction_outcome"),)
    blobs = {item.sha256: data for item, data in zip(records, images + [outcome])}
    verdict = verify_evidence(records, None, NOW, lambda item: blobs[item.sha256])
    assert verdict.status == "complete"
    statuses = {viewport: True for viewport in DEFAULT_VIEWPORTS}
    return RunRecord(
        planned.arm,
        planned.scenario,
        planned.repetition,
        planned.run_id,
        planned.session_id,
        planned.profile_key,
        "implementer-" + planned.run_id,
        "reviewer-" + planned.run_id,
        True,
        planned.spec,
        True,
        2,
        0,
        0,
        statuses.copy(),
        statuses.copy(),
        statuses.copy(),
        1,
        0,
        100,
        1,
        (
            usage(planned.run_id + "-implementation"),
            usage(planned.run_id + "-review", "review"),
        ),
        30,
        10,
        20,
        verdict,
        records,
    )


@pytest.fixture
def runs():
    return [record(planned) for planned in schedule(SPEC)]


def result(runs, spec=SPEC, prices=PRICES, now=NOW):
    return select(spec, runs, prices, now=now)


def incomplete(runs, **kwargs):
    verdict = result(runs, **kwargs)
    assert verdict.status == "incomplete" and verdict.reasons
    assert verdict.selected_arm is None and not verdict.scenario_routes
    return " ".join(verdict.reasons)


def transform(runs, arm, **changes):
    return [replace(run, **changes) if run.arm == arm else run for run in runs]


def test_schedule_deterministic_varies_and_has_distinct_reservations():
    planned = schedule(SPEC)
    assert planned == schedule(SPEC)
    assert len(planned) == 4 * 3 * 2
    assert all(run.spec is SPEC for run in planned)
    for field in ("run_id", "session_id", "profile_key"):
        assert len({getattr(run, field) for run in planned}) == len(planned)
    assert {(run.scenario, run.repetition, run.arm) for run in planned} == {
        (scenario, repetition, arm)
        for scenario in SCENARIOS
        for repetition in range(3)
        for arm in ARMS
    }
    orders = {
        tuple(run.arm for run in schedule(replace(SPEC, seed=seed)))
        for seed in range(10)
    }
    assert len(orders) > 1
    for first, second in zip(planned[::2], planned[1::2]):
        assert (first.scenario, first.repetition) == (
            second.scenario,
            second.repetition,
        )
        assert first.arm != second.arm


@pytest.mark.parametrize(
    "field,value",
    (
        ("app_commit", None),
        ("app_commit", "A" * 40),
        ("app_commit", "a" * 39),
        ("app_commit", "a" * 40 + "\n"),
        ("source_url", ""),
        ("source_url", None),
        ("source_url", "https://u@a.test"),
        ("source_url", "https://a.test:bad"),
        ("source_url", "http://169.254.169.254"),
        ("brief_sha256", ""),
        ("brief_sha256", "b" * 63),
        ("model", None),
        ("effort", ""),
        ("output_contract", 4),
        ("runs_per_arm", 0),
        ("runs_per_arm", -1),
        ("runs_per_arm", True),
        ("runs_per_arm", 1.5),
        ("seed", None),
        ("seed", -1),
        ("seed", float("nan")),
    ),
)
def test_invalid_spec(field, value):
    with pytest.raises(ValueError):
        replace(SPEC, **{field: value})


@pytest.mark.parametrize("spec", (None, {}, "", []))
def test_missing_spec(spec, runs):
    with pytest.raises(ValueError):
        schedule(spec)
    assert "TrialSpec" in incomplete(runs, spec=spec)


def test_no_selection_when_everything_ties(runs):
    verdict = result(runs)
    assert verdict.status == "no_selection" and verdict.selected_arm is None
    assert not verdict.scenario_routes
    assert all(winner is None for winner in verdict.scenario_winners.values())
    assert "tie" in verdict.reasons[-1]


@pytest.mark.parametrize(
    "changes,criterion",
    (
        ({"accepted": False}, "accepted_completion_rate"),
        ({"missed_defects": 1}, "missed_defects_plus_regressions"),
        ({"introduced_regressions": 1}, "missed_defects_plus_regressions"),
        ({"correction_rounds": 2}, "correction_rounds"),
        ({"human_interventions_post_start": 1}, "human_interventions_post_start"),
        ({"elapsed_monotonic_s": 200}, "median_elapsed_monotonic_s"),
    ),
)
def test_each_declared_criterion_decides(runs, changes, criterion):
    verdict = result(transform(runs, "cli", **changes))
    assert verdict.status == "selected" and verdict.selected_arm == "mcp"
    assert all(winner == "mcp" for winner in verdict.scenario_winners.values())
    assert all(criterion in reason for reason in verdict.reasons)


def test_cost_decides_before_elapsed(runs):
    changed = [
        replace(
            run,
            usage=tuple(replace(item, output_tokens=40) for item in run.usage),
            elapsed_monotonic_s=1,
        )
        if run.arm == "cli"
        else run
        for run in runs
    ]
    verdict = result(changed)
    assert verdict.status == "selected" and verdict.selected_arm == "mcp"
    assert "list_cost_usd" in verdict.reasons[-1]


def test_lexicographic_completion_dominates_defects_cost_and_time(runs):
    changed = transform(runs, "cli", accepted=False)
    changed = transform(
        changed,
        "mcp",
        missed_defects=2,
        introduced_regressions=100,
        correction_rounds=100,
        human_interventions_post_start=100,
        elapsed_monotonic_s=1000,
    )
    assert result(changed).selected_arm == "mcp"


def test_conditional_routing_and_ties_route_to_overall(runs):
    changed = [
        replace(run, accepted=False)
        if (run.scenario == "layout" and run.arm == "cli")
        or (run.scenario == "interaction" and run.arm == "mcp")
        or (run.scenario == "diagnostics" and run.arm == "cli")
        else run
        for run in runs
    ]
    verdict = result(changed)
    assert verdict.status == "conditional_routing" and verdict.selected_arm is None
    assert verdict.scenario_routes == {
        "layout": "mcp",
        "interaction": "cli",
        "diagnostics": "mcp",
        "review": "mcp",
    }
    assert verdict.overall_winner == "mcp"


def test_disagreement_with_overall_tie_refuses_tied_route(runs):
    changed = [
        replace(run, accepted=False)
        if (run.scenario == "layout" and run.arm == "cli")
        or (run.scenario == "interaction" and run.arm == "mcp")
        else run
        for run in runs
    ]
    verdict = result(changed)
    assert verdict.status == "no_selection" and not verdict.scenario_routes
    assert "no overall winner" in verdict.reasons[-1]


def test_disagreement_without_tied_scenarios_can_route_with_overall_tie(runs):
    changed = [
        replace(run, accepted=False)
        if (run.scenario in ("layout", "interaction") and run.arm == "cli")
        or (run.scenario in ("diagnostics", "review") and run.arm == "mcp")
        else run
        for run in runs
    ]
    verdict = result(changed)
    assert verdict.status == "conditional_routing" and len(verdict.scenario_routes) == 4
    assert verdict.overall_winner is None


@pytest.mark.parametrize(
    "elapsed,expected",
    ((95, "no_selection"), (94.999, "selected"), (100, "no_selection")),
)
def test_relative_tolerance_exact_boundary(runs, elapsed, expected):
    verdict = result(transform(runs, "mcp", elapsed_monotonic_s=elapsed))
    assert RELATIVE_TOLERANCE == Decimal("0.05")
    assert verdict.status == expected


def test_cost_tolerance_boundary(runs):
    # 100 total billed tokens at USD 1/million versus 95: exactly 5%.
    changed = []
    for run in runs:
        tokens = 95 if run.arm == "mcp" else 100
        items = tuple(
            replace(
                item,
                input_tokens=tokens,
                cache_read_tokens=0,
                cache_write_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
            )
            for item in run.usage
        )
        changed.append(replace(run, usage=items))
    assert result(changed).status == "no_selection"
    cheaper = [
        replace(run, usage=tuple(replace(item, input_tokens=94) for item in run.usage))
        if run.arm == "mcp"
        else run
        for run in changed
    ]
    assert result(cheaper).selected_arm == "mcp"


def test_usage_no_double_count_and_both_phases():
    first = usage(
        "1",
        input_tokens=100,
        cache_read_tokens=10,
        cache_write_tokens=20,
        output_tokens=20,
        reasoning_tokens=5,
    )
    second = usage(
        "2",
        "review",
        input_tokens=100,
        cache_read_tokens=10,
        cache_write_tokens=20,
        output_tokens=15,
        reasoning_tokens=5,
        reasoning_included_in_output=False,
    )
    totals = account_usage(
        (first, second), {"model": dict(zip(TOKEN_CLASSES, (1, 2, 3, 4)))}
    )
    assert (
        totals.input_tokens,
        totals.cache_read_tokens,
        totals.cache_write_tokens,
        totals.output_tokens,
    ) == (140, 20, 40, 40)
    assert totals.list_cost_usd == Decimal("0.00046")


def test_usage_zero_boundary():
    item = usage(
        "zero",
        input_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
    )
    assert account_usage((item,), PRICES).list_cost_usd == 0


@pytest.mark.parametrize("name", TOKEN_CLASSES + ("reasoning_tokens",))
@pytest.mark.parametrize("value", (None, -1, 0.5, True, float("nan")))
def test_bad_token_counts(name, value):
    with pytest.raises(ValueError):
        usage("1", **{name: value})


@pytest.mark.parametrize(
    "changes",
    (
        {"request_id": ""},
        {"request_id": None},
        {"model": ""},
        {"phase": None},
        {"phase": "tool"},
        {"reasoning_included_in_output": None},
        {"reasoning_included_in_output": 1},
        {"cache_read_tokens": 101},
        {"reasoning_tokens": 21},
    ),
)
def test_invalid_usage_record(changes):
    with pytest.raises(ValueError):
        replace(usage("1"), **changes)


@pytest.mark.parametrize("records", (None, [], (), (None,), ""))
def test_empty_or_invalid_accounting(records):
    with pytest.raises(ValueError):
        account_usage(records, PRICES)


@pytest.mark.parametrize(
    "table",
    (
        None,
        {},
        {"other": PRICES["model"]},
        {"model": {}},
        {"model": {name: None for name in TOKEN_CLASSES}},
        {"model": {name: -1 for name in TOKEN_CLASSES}},
        {"model": {name: float("nan") for name in TOKEN_CLASSES}},
        {"model": {name: True for name in TOKEN_CLASSES}},
    ),
)
def test_missing_invalid_prices_are_not_zero(table, runs):
    with pytest.raises(ValueError):
        account_usage((usage("1"),), table)
    incomplete(runs, prices=table)


def test_duplicate_request_ids_in_run_and_trial(runs):
    with pytest.raises(ValueError, match="duplicate"):
        account_usage((usage("1"), usage("1", "review")), PRICES)
    changed = list(runs)
    changed[0] = replace(
        changed[0],
        usage=(
            changed[0].usage[0],
            replace(changed[0].usage[1], request_id=changed[0].usage[0].request_id),
        ),
    )
    assert "duplicate" in incomplete(changed)
    changed = list(runs)
    changed[1] = replace(changed[1], usage=(changed[0].usage[0], changed[1].usage[1]))
    assert "across trial" in incomplete(changed)


@pytest.mark.parametrize("missing", (None, [], (), "", [None]))
def test_missing_runs(missing):
    incomplete(missing)


def test_missing_and_duplicate_scheduled_runs(runs):
    assert "missing" in incomplete(runs[:-1])
    assert "duplicate" in incomplete(runs + [runs[0]])


@pytest.mark.parametrize("name", COUNT_FIELDS + ("repetition",))
@pytest.mark.parametrize("value", (None, -1, 1.5, True, float("nan")))
def test_all_run_counts_fail_closed(runs, name, value):
    runs[0] = replace(runs[0], **{name: value})
    incomplete(runs)


@pytest.mark.parametrize("name", ("elapsed_monotonic_s", "cold_start_s"))
@pytest.mark.parametrize("value", (None, -1, "", True, float("nan"), float("inf")))
def test_bad_durations(runs, name, value):
    runs[0] = replace(runs[0], **{name: value})
    incomplete(runs)


@pytest.mark.parametrize(
    "changes",
    (
        {"arm": None},
        {"scenario": []},
        {"repetition": 3},
        {"spec": None},
        {"spec": replace(SPEC, effort="other")},
        {"session_id": ""},
        {"profile_key": None},
        {"run_id": "other"},
        {"reviewer_blind": False},
        {"reviewer_blind": 1},
        {"accepted": None},
        {"missed_defects": 3},
        {"cold_start_s": 101},
        {"browser_tool_payload_bytes": 31},
        {"evidence_verdict": None},
        {"evidence_verdict": EvidenceVerdict("incomplete")},
        {"evidence_records": ()},
        {"evidence_records": (None,)},
        {"usage": ()},
    ),
)
def test_invalid_run_fields(runs, changes):
    runs[0] = replace(runs[0], **changes)
    incomplete(runs)


@pytest.mark.parametrize(
    "field", ("functional_pass", "accessibility_pass", "visual_pass")
)
@pytest.mark.parametrize(
    "statuses",
    (
        None,
        {},
        {DEFAULT_VIEWPORTS[0]: True},
        {v: None for v in DEFAULT_VIEWPORTS},
        {v: "skipped" for v in DEFAULT_VIEWPORTS},
        {v: False for v in DEFAULT_VIEWPORTS},
        {v: 1 for v in DEFAULT_VIEWPORTS},
    ),
)
def test_viewport_checks_not_skipped(runs, field, statuses):
    runs[0] = replace(runs[0], **{field: statuses})
    incomplete(runs)


def test_reviewer_and_shared_sessions_refused(runs):
    changed = list(runs)
    changed[0] = replace(
        changed[0], reviewer_session_id=changed[0].implementer_session_id
    )
    incomplete(changed)
    changed[0] = replace(runs[0], reviewer_session_id=runs[0].session_id)
    incomplete(changed)
    changed = list(runs)
    changed[1] = replace(
        changed[1], implementer_session_id=changed[0].reviewer_session_id
    )
    assert "shared session" in incomplete(changed)
    changed[1] = replace(runs[1], profile_key=runs[0].profile_key)
    assert "profile" in incomplete(changed)


def test_snapshot_cannot_establish_visual_acceptance(runs):
    snapshots = tuple(
        replace(item, kind="accessibility_snapshot", viewport=None)
        if item.kind == "screenshot"
        else item
        for item in runs[0].evidence_records
    )
    runs[0] = replace(runs[0], evidence_records=snapshots)
    assert "screenshot" in incomplete(runs)


@pytest.mark.parametrize(
    "change",
    (
        {"upload_status": "pending"},
        {"upload_status": "failed"},
        {"expires_at": "2026-10-01T00:00:00Z"},
        {"capture_time": "2026-10-01T00:01:00Z"},
        {"session_id": "another"},
        {"app_commit": "c" * 40},
        {"source_url": "https://another.test"},
        {"run_id": "another"},
    ),
)
def test_evidence_boundary_and_identity(runs, change):
    records = list(runs[0].evidence_records)
    records[0] = replace(records[0], **change)
    runs[0] = replace(runs[0], evidence_records=tuple(records))
    incomplete(runs)


def test_missing_and_duplicate_screenshot_evidence(runs):
    changed = list(runs)
    changed[0] = replace(changed[0], evidence_records=changed[0].evidence_records[1:])
    incomplete(changed)
    changed[0] = replace(
        runs[0],
        evidence_records=runs[0].evidence_records + (runs[0].evidence_records[0],),
    )
    assert "duplicate evidence" in incomplete(changed)


def test_phase_model_seeded_defects_and_task_comparability(runs):
    changed = list(runs)
    changed[0] = replace(changed[0], usage=(changed[0].usage[0],))
    assert "both required" in incomplete(changed)
    changed[0] = replace(
        runs[0], usage=tuple(replace(item, model="other") for item in runs[0].usage)
    )
    assert "model differs" in incomplete(
        changed, prices={**PRICES, "other": PRICES["model"]}
    )
    changed[0] = replace(runs[0], seeded_defects_total=3)
    assert "seeded defect" in incomplete(changed)
    changed[0] = replace(
        runs[0],
        evidence_records=tuple(
            replace(item, task_id="other") for item in runs[0].evidence_records
        ),
    )
    assert "mixed tasks" in incomplete(changed)


def test_payload_bytes_stay_outside_tokens_and_cost(runs):
    baseline = result(runs)
    inflated = result(
        transform(
            runs,
            "mcp",
            browser_payload_bytes=10000,
            tool_payload_bytes=20000,
            browser_tool_payload_bytes=30000,
        )
    )
    assert inflated.status == baseline.status
    assert (
        inflated.metrics["overall"]["mcp"].list_cost_usd
        == baseline.metrics["overall"]["mcp"].list_cost_usd
    )
    assert inflated.metrics["overall"]["mcp"].browser_payload_bytes == 120000
    assert inflated.metrics["overall"]["mcp"].tool_payload_bytes == 240000
