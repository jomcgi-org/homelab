from __future__ import annotations

import json
from datetime import datetime, timezone

from factory.orchestration import retro

NOW = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
TASK = "t-aaaaaaaa-1111"
OTHER = "t-bbbbbbbb-2222"


def _run(task, node, attempt, status="succeeded", outcome=None, run_id=None):
    return {
        "id": run_id,
        "task_id": task,
        "node_key": node,
        "attempt": attempt,
        "status": status,
        "model": "opus",
        "cost_usd": 1.0,
        "outcome_json": json.dumps(outcome or {}),
        "created_at": NOW,
        "finished_at": NOW,
    }


def _turn(task, node, attempt, cost=0.5, commands=(), result="done", denials=None):
    return {
        "local_session_id": f"factory:{task}:{node}:{attempt}",
        "list_cost_usd": cost,
        "terminal_reason": "completed",
        "permission_denials": json.dumps(denials or []),
        "usage_json": json.dumps(
            {"activities": [{"type": "bash", "command": c} for c in commands]}
        ),
        "result_head": result,
    }


def _data(**overrides):
    death = {
        "reason": "guest_cessation_confirmed: exact control-plane cessation",
        "cessation": {"state": "evicted"},
    }
    repair = {
        "artifact": {"value": {"reason": "Repair after guest_cessation_confirmed"}}
    }
    data = {
        "runs": [
            _run(TASK, "conductor_1", 1),
            _run(TASK, "implement_x", 1, "failed", death, run_id=7),
            _run(TASK, "conductor_2", 1, outcome=repair),
            _run(OTHER, "correct_1", 1),
        ],
        "receipts": [
            {"task_id": TASK, "issue_number": 101, "state": "admitted"},
            {"task_id": OTHER, "issue_number": 202, "state": "succeeded"},
        ],
        "refusals": [
            {
                "task_id": TASK,
                "detail_json": json.dumps(
                    {
                        "refusal_code": "bound_exceeds_policy",
                        "cause": "factory-decision:conductor_1:1",
                        "reason": "edit 0 (implement_x): max_cost_usd exceeds policy",
                    }
                ),
            }
        ],
        "verdicts": [{"review_run_id": 7, "verdict": "blocked", "summary": ""}],
        "turns": [
            _turn(TASK, "conductor_1", 1, cost=0.75),
            _turn(TASK, "conductor_2", 1, cost=1.25),
            _turn(
                OTHER,
                "correct_1",
                1,
                commands=("pip install pytest", "curl https://get.helm.sh/helm.tgz"),
            ),
        ],
        "task_costs": {TASK: 2.0, OTHER: 0.5},
        "task_prs": {TASK: 9001},
        "pr_sizes": {9001: 20},
        "published": {f"factory:{TASK}:conductor_1:1": 101},
        "github": {
            "retro": [{"number": 7000, "state": "open", "title": "factory: old"}],
            "open": [{"number": 7001, "title": "factory: something"}],
        },
    }
    data.update(overrides)
    return data


def test_digest_cites_public_pages_only_where_they_exist():
    digest = retro.build_retro_digest(_data(), NOW)

    assert f"{retro.PUBLIC_BASE}/101/conductor_1/1" in digest
    # Not published: falls back to issue, node, attempt and task id.
    assert "#202 correct_1/1 (task t-bbbbbbbb)" in digest
    assert retro.RETRO_MARKER in digest


def test_digest_prices_refusals_deaths_repairs_and_setup_calls():
    digest = retro.build_retro_digest(_data(), NOW)

    assert "bound_exceeds_policy: 1 refusals on 1 tasks, $0.75 list" in digest
    assert "1 of 4 node runs ended in an infrastructure death" in digest
    assert "guest state evicted: 1" in digest
    assert "1 successful planner runs cite an infra death" in digest
    assert "$1.25 list" in digest
    assert "pip install (pytest, requirements): 1 sessions" in digest
    assert "helm download: 1 sessions" in digest
    assert "plus 1 recorded as blocked that were infra deaths" in digest
    assert "PR #9001: $2.00 for 20 lines" in digest


def test_digest_lists_existing_issues_for_dedupe():
    digest = retro.build_retro_digest(_data(), NOW)

    assert "#7000 [open] factory: old" in digest
    assert "#7001 factory: something" in digest
    unavailable = retro.build_retro_digest(_data(github={"error": "HTTPError"}), NOW)
    assert "GitHub issue list unavailable (HTTPError)" in unavailable


def test_digest_is_bounded():
    many = [
        _turn(f"t-{i:08d}-x", "implement_y", 1, commands=("pip install pytest",) * 3)
        for i in range(3000)
    ]
    digest = retro.build_retro_digest(_data(turns=many), NOW)

    assert len(digest) <= retro.DIGEST_MAX_CHARS + 100


def test_failures_show_what_the_model_said_and_group_missing_tools():
    missing = {"reason": "artifact_missing: no diff recorded for .factory/x.json"}
    runs = [
        _run(TASK, "conductor_5", 1, "failed", missing),
        _run(OTHER, "conductor_6", 1, "failed", missing),
        _run(OTHER, "conductor_6", 2, "failed", missing),
    ]
    turns = [
        _turn(
            TASK, "conductor_5", 1, result="Blocked: `/usr/local/bin/host` is missing."
        ),
        _turn(
            OTHER,
            "conductor_6",
            1,
            result="Could not write: `/usr/local/bin/host` is missing",
        ),
        _turn(
            OTHER,
            "conductor_6",
            2,
            result="Could not write: `/usr/local/bin/host` is missing",
        ),
    ]
    digest = retro.build_retro_digest(_data(runs=runs, turns=turns), NOW)

    assert (
        "missing in the guest: `/usr/local/bin/host` named by 3 turns on 2 tasks"
        in digest
    )
    assert (
        '2 of these said: "Could not write: `/usr/local/bin/host` is missing"' in digest
    )


def test_review_scorecards_keep_first_pass_and_rounds_separate():
    verdicts = [
        {
            "task_id": f"t-{i}",
            "review_run_id": i,
            "sample_kind": "delivery",
            "task_class": "bug-fix",
            "verdict": verdict,
        }
        for i, verdict in enumerate(
            ["approve", "changes_requested", "blocked", "unparseable"], 1
        )
    ]
    runs = [
        _run("t-1", "review_1", 1, outcome={"value": {"verdict": "approve"}}, run_id=1),
        _run(
            "t-2",
            "review_1",
            1,
            outcome={"value": {"verdict": "changes_requested"}},
            run_id=2,
        ),
        _run("t-3", "review_1", 1, "failed", run_id=3),
        _run("t-4", "review_1", 1, run_id=4),
        _run("t-2", "review_2", 1, outcome={"value": {"verdict": "approve"}}, run_id=5),
        _run("t-2", "correct_1", 1, "failed"),
        _run("t-2", "correct_1", 2),
        _run("t-2", "correct_2", 1),
    ]
    digest = retro.build_retro_digest(
        _data(runs=runs, verdicts=verdicts + [verdicts[0]]), NOW
    )
    assert (
        "first-pass scorecard: operational approvals 1/4; quality approvals 1/2; blocked 1; unknown 1"
        in digest
    )
    assert (
        "first-pass delivery/bug-fix/opus: operational approvals 1/4; quality approvals 1/2"
        in digest
    )
    assert (
        "all terminal review attempts: operational approvals 2/5; quality approvals 2/3"
        in digest
    )
    assert "correction rounds 2; attempts 3; retry attempts 1" in digest


def test_review_scorecard_counts_non_object_outcomes_as_unknown():
    runs = []
    for i, value in enumerate([[], None, 1], 1):
        run = _run(TASK, "review_1", i, run_id=i)
        run["outcome_json"] = json.dumps(value)
        runs.append(run)
    digest = retro.build_retro_digest(_data(runs=runs, verdicts=[]), NOW)
    assert (
        "all terminal review attempts: operational approvals 0/3; quality approvals 0/0; blocked 0; unknown 3"
        in digest
    )
