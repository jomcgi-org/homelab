"""Fixture contracts for the actual, manually invoked PostgreSQL outcomes report."""

from __future__ import annotations

import itertools
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import text

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = START + timedelta(days=1)
AS_OF = START + timedelta(days=19)
MERGED = START + timedelta(days=2)
HEAD = "a" * 40
MERGE_SHA = "b" * 40
REPORT = Path(__file__).with_name("outcomes_report.sql")
SECTIONS = (
    "Task outcomes by task class and initiating model",
    "Contributions by task class role and actual model",
    "Difficulty bands",
    "Coverage",
)
STATES = (
    "positives",
    "reverted",
    "ci_failed",
    "pending_maturity",
    "unknown",
    "failed",
    "censored",
)


def _report(session, *, as_of=AS_OF):
    """Execute each section from disk with bound values and a rolled-back SET.

    SET affects future transactions even inside a writable fixture transaction.
    Roll its SAVEPOINT back in finally, including on a report failure, so the
    shared harness never inherits default_transaction_read_only from this file.
    """
    source = REPORT.read_text()
    chunks = re.split(r"^\\echo '== ([^']+)'\s*$", source, flags=re.MULTILINE)
    assert tuple(chunks[1::2]) == SECTIONS
    prefix = "\n".join(
        line for line in chunks[0].splitlines() if not line.startswith("\\")
    )
    before = session.execute(text("SHOW default_transaction_read_only")).scalar_one()
    savepoint = session.begin_nested()
    try:
        session.execute(text(prefix))
        assert (
            session.execute(text("SHOW default_transaction_read_only")).scalar_one()
            == "on"
        )
        result = {}
        for name, sql in zip(chunks[1::2], chunks[2::2], strict=True):
            sql = "\n".join(
                line for line in sql.splitlines() if not line.startswith("\\")
            )
            assert sql.strip().endswith(";") and sql.count(";") == 1
            sql, substitutions = re.subn(
                r":'(cohort_start|cohort_end|as_of)'::timestamptz",
                r"CAST(:\1 AS timestamptz)",
                sql,
            )
            assert substitutions == 3
            assert not re.search(r":'[a-z_]+\b", sql)
            result[name] = (
                session.execute(
                    text(sql),
                    {"cohort_start": START, "cohort_end": END, "as_of": as_of},
                )
                .mappings()
                .all()
            )
        return result
    finally:
        savepoint.rollback()
        assert (
            session.execute(text("SHOW default_transaction_read_only")).scalar_one()
            == before
        )


class Rows:
    """Minimal source rows, with explicit timestamps and real migration constraints."""

    def __init__(self, session):
        self.session = session
        self.ids = itertools.count(100000)

    def task(
        self,
        *,
        created=START,
        settled=None,
        task_class="bug-fix",
        repo="jomcgi-org/homelab",
        **receipt,
    ):
        number = next(self.ids)
        task = f"outcomes-{number}"
        self.session.execute(
            text("""
                INSERT INTO swarm.swarm_task
                    (id, created_at, task_text, repo, conductor_model, settled_at)
                VALUES (:id, :created, 'fixture', :repo, 'conductor', :settled)
            """),
            {"id": task, "created": created, "settled": settled, "repo": repo},
        )
        self.session.execute(
            text("""
                INSERT INTO swarm.factory_receipt
                    (repo, issue_number, title, body, url, actor, task_id,
                     task_class, state, created_at, updated_at, escalation_json)
                VALUES (:repo, :number, 'fixture', '', '', 'test',
                        :task, :class, 'admitted', :created, :updated, :escalation)
            """),
            {
                "repo": repo,
                "number": number,
                "task": task,
                "class": task_class,
                "created": created,
                "updated": receipt.get("updated", created),
                "escalation": receipt.get("escalation", "{}"),
            },
        )
        return task

    def audit(self, task, action, *, at=MERGED, **detail):
        self.session.execute(
            text("""
                INSERT INTO swarm.factory_audit (actor, action, task_id, detail_json, created_at)
                VALUES ('test', :action, :task, :detail, :at)
            """),
            {"action": action, "task": task, "detail": json.dumps(detail), "at": at},
        )

    def merge(self, task, *, at=MERGED, conclusion="success", window=True, pr=123):
        self.audit(task, "merged", at=at, pr_number=pr, merge_commit_sha=MERGE_SHA)
        if conclusion is not None:
            self.audit(
                task,
                "merge_ci",
                at=at + timedelta(minutes=1),
                pr_number=pr,
                head_sha=HEAD,
                conclusion=conclusion,
            )
        if window:
            self.audit(
                task,
                "revert_window_closed",
                at=at + timedelta(days=7),
                pr_number=pr,
                merge_commit_sha=MERGE_SHA,
            )

    def session_row(self, task, *, model="model-a", named=False):
        local_id = f"factory:{task}:fixture" if named else f"fixture:{next(self.ids)}"
        return self.session.execute(
            text("""
                INSERT INTO agent_sessions.agent_sessions
                    (local_session_id, workspace, branch, model, created_at, last_turn_at)
                VALUES (:local, '/tmp/outcomes', 'fixture', :model, :at, :at)
                RETURNING id
            """),
            {"local": local_id, "model": model, "at": START},
        ).scalar_one()

    def turn(self, session_id, *, cost=2, model="model-a", at=START, seq=1):
        self.session.execute(
            text("""
                INSERT INTO agent_sessions.agent_turns
                    (session_id, seq, prompt, result_text, list_cost_usd, model, created_at)
                VALUES (:session, :seq, 'fixture', '', :cost, :model, :at)
            """),
            {"session": session_id, "seq": seq, "cost": cost, "model": model, "at": at},
        )

    def run(
        self,
        task,
        *,
        node="implement",
        attempt=1,
        model="model-a",
        status="succeeded",
        at=START,
        session_id=None,
        finished=None,
        pin=None,
    ):
        dispatch = f"{node}:{attempt}"
        self.session.execute(
            text("""
                INSERT INTO swarm.swarm_node_run
                    (task_id, node_key, attempt, dispatch_key, model, status,
                     session_id, created_at, finished_at, pin_json)
                VALUES (:task, :node, :attempt, :dispatch, :model, :status,
                        :session, :at, :finished, :pin)
            """),
            {
                "task": task,
                "node": node,
                "attempt": attempt,
                "dispatch": dispatch,
                "model": model,
                "status": status,
                "session": session_id,
                "at": at,
                "finished": finished
                if finished is not None
                else at + timedelta(hours=1),
                "pin": json.dumps(pin) if pin is not None else None,
            },
        )
        return dispatch

    def start(
        self,
        task,
        *,
        key="implement:1",
        status="succeeded",
        cost=3,
        maximum=10,
        session_id=None,
        model="model-a",
        at=START,
        updated=START,
        basis=None,
    ):
        self.session.execute(
            text("""
                INSERT INTO swarm.factory_start
                    (task_id, start_key, actor, model, max_cost_usd, status, cost_usd,
                     session_id, created_at, updated_at, accounting_basis)
                VALUES (:task, :key, 'test', :model, :maximum, :status, :cost,
                        :session, :at, :updated, :basis)
            """),
            {
                "task": task,
                "key": key,
                "model": model,
                "maximum": maximum,
                "status": status,
                "cost": cost,
                "session": session_id,
                "at": at,
                "updated": updated,
                "basis": basis,
            },
        )

    def metadata(self, *, pr=123, lines=49, files=1):
        self.session.execute(
            text("""
                INSERT INTO observability.merged_prs
                    (number, title, merged_at, additions, deletions, changed_files,
                     type, agent_authored, snapshotted_at)
                VALUES (:pr, 'fixture', :merged, :additions, 1, :files, 'feat', true, :merged)
            """),
            {"pr": pr, "merged": MERGED, "additions": lines - 1, "files": files},
        )

    def labels(self, task, labels, *, updated=START):
        work_item = self.session.execute(
            text("""
                INSERT INTO swarm.work_item
                    (title, state, source_kind, trust, labels, created_at, updated_at)
                VALUES ('fixture', 'ready', 'factory', 'trusted', CAST(:labels AS jsonb), :at, :updated)
                RETURNING id
            """),
            {"labels": json.dumps(labels), "at": START, "updated": updated},
        ).scalar_one()
        self.session.execute(
            text(
                "UPDATE swarm.factory_receipt SET work_item_id = :work_item WHERE task_id = :task"
            ),
            {"task": task, "work_item": work_item},
        )


@pytest.fixture
def rows(session):
    return Rows(session)


def _single(report, section=0):
    values = report[SECTIONS[section]]
    assert len(values) == 1
    return values[0]


def _state(report, expected):
    row = _single(report)
    assert row["tasks"] == 1
    assert {state: row[state] for state in STATES} == {
        state: int(state == expected) for state in STATES
    }
    assert _single(report, 3)["tasks"] == 1
    bands = report[SECTIONS[2]]
    assert len(bands) == 4
    assert all(b["tasks"] == 1 and b["positives"] == row["positives"] for b in bands)
    return row


def test_file_is_read_only_and_has_four_bound_selects(session):
    source = REPORT.read_text()
    sql = re.sub(r"--[^\n]*", "", source)
    sql = "\n".join(line for line in sql.splitlines() if not line.startswith("\\"))
    assert re.search(r"SET\s+default_transaction_read_only\s*=\s*on\s*;", sql)
    assert not re.search(
        r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", sql, re.IGNORECASE
    )
    assert set(re.findall(r":'([a-z_]+)'", sql)) == {
        "cohort_start",
        "cohort_end",
        "as_of",
    }
    report = _report(session)
    assert report[SECTIONS[0]] == report[SECTIONS[1]] == report[SECTIONS[2]] == []
    assert _single(report, 3)["tasks"] == 0
    # A second execution and write also prove that the report's SET did not leak.
    Rows(session).task()
    assert _single(_report(session))["tasks"] == 1


def test_mature_positive_requires_ci_and_closed_window(session, rows):
    task = rows.task()
    rows.merge(task)
    row = _state(_report(session), "positives")
    assert (
        row["success_numerator"],
        row["success_denominator"],
        row["success_rate"],
    ) == (1, 1, 1)
    assert (row["revert_numerator"], row["revert_denominator"], row["revert_rate"]) == (
        0,
        1,
        0,
    )
    assert row["hours_per_positive"] == 48


def test_immature_merge_is_pending(session, rows):
    rows.merge(rows.task(), at=AS_OF - timedelta(days=1), window=False)
    row = _state(_report(session), "pending_maturity")
    assert row["success_unknown_count"] == row["revert_unknown_count"] == 1
    assert _single(_report(session), 3)["pending_maturity"] == 1


def test_revert_inside_window_wins_over_ci_and_close(session, rows):
    task = rows.task()
    rows.merge(task)
    rows.audit(task, "reverted", at=MERGED + timedelta(days=3), pr_number=123)
    row = _state(_report(session), "reverted")
    assert (row["revert_numerator"], row["revert_denominator"], row["revert_rate"]) == (
        1,
        1,
        1,
    )


def test_failed_ci_is_not_positive(session, rows):
    rows.merge(rows.task(), conclusion="failure")
    row = _state(_report(session), "ci_failed")
    assert row["success_denominator"] == 1
    assert row["success_rate"] == 0


@pytest.mark.parametrize("conclusion", ["unknown", "pending", "none", None])
def test_missing_or_unjudged_ci_stays_unknown(session, rows, conclusion):
    rows.merge(rows.task(), conclusion=conclusion)
    row = _state(_report(session), "unknown")
    assert row["success_unknown_count"] == 1
    assert _single(_report(session), 3)["unknown_ci_evidence"] == 1


@pytest.mark.parametrize(
    "reference", ["finish_task", "delivery_ready", "run", "nested_run"]
)
def test_historical_merge_without_landing_audits_is_unknown(session, rows, reference):
    task = rows.task()
    rows.metadata()
    if reference in ("run", "nested_run"):
        rows.run(task)
        outcome = {"pr_number": 123}
        if reference == "nested_run":
            outcome = {"value": outcome}
        session.execute(
            text(
                "UPDATE swarm.swarm_node_run SET outcome_json = :outcome WHERE task_id = :task"
            ),
            {"outcome": json.dumps(outcome), "task": task},
        )
    else:
        rows.audit(
            task,
            reference,
            evidence={"pr_url": "https://github.com/jomcgi-org/homelab/pull/123"},
        )
    _state(_report(session), "unknown")
    assert _single(_report(session), 3)["historical_unjudged_merges"] == 1


@pytest.mark.parametrize("at", [AS_OF, AS_OF + timedelta(seconds=1)])
def test_late_close_and_revert_are_ignored(session, rows, at):
    task = rows.task()
    rows.merge(task, window=False)
    rows.audit(
        task, "revert_window_closed", at=at, pr_number=123, merge_commit_sha=MERGE_SHA
    )
    rows.audit(task, "reverted", at=at, pr_number=123)
    row = _state(_report(session), "pending_maturity")
    assert row["revert_numerator"] == row["revert_denominator"] == 0
    assert row["revert_unknown_count"] == 1


@pytest.mark.parametrize(
    "bad_evidence",
    ["wrong_pr", "invalid_head", "missing_head", "before_merge", "malformed"],
)
def test_ci_evidence_must_match_delivery_and_have_valid_head(
    session, rows, bad_evidence
):
    task = rows.task()
    rows.merge(task, conclusion=None)
    detail = {"pr_number": 123, "head_sha": HEAD, "conclusion": "success"}
    at = MERGED + timedelta(minutes=1)
    if bad_evidence == "wrong_pr":
        detail["pr_number"] = 124
    elif bad_evidence == "invalid_head":
        detail["head_sha"] = "not-a-head"
    elif bad_evidence == "missing_head":
        del detail["head_sha"]
    elif bad_evidence == "before_merge":
        at = MERGED - timedelta(seconds=1)
    rows.audit(task, "merge_ci", at=at, **detail)
    if bad_evidence == "malformed":
        session.execute(
            text(
                "UPDATE swarm.factory_audit SET detail_json = '{broken' WHERE task_id = :task AND action = 'merge_ci'"
            ),
            {"task": task},
        )
    _state(_report(session), "unknown")


@pytest.mark.parametrize(
    "bad_evidence", ["wrong_pr", "wrong_merge", "missing_merge", "early"]
)
def test_window_close_requires_matching_delivery_and_seven_days(
    session, rows, bad_evidence
):
    task = rows.task()
    rows.merge(task, window=False)
    detail = {"pr_number": 123, "merge_commit_sha": MERGE_SHA}
    at = MERGED + timedelta(days=7)
    if bad_evidence == "wrong_pr":
        detail["pr_number"] = 124
    elif bad_evidence == "wrong_merge":
        detail["merge_commit_sha"] = "c" * 40
    elif bad_evidence == "missing_merge":
        del detail["merge_commit_sha"]
    else:
        at -= timedelta(seconds=1)
    rows.audit(task, "revert_window_closed", at=at, **detail)
    _state(_report(session), "pending_maturity")


def test_retries_models_corrections_and_review_are_accounted(session, rows):
    task = rows.task()
    rows.merge(task)
    rows.run(task, status="failed")
    # Attempt 2 stepped up pools: the stored pin marker is what counts as
    # escalation, not the model switch alone.
    rows.run(
        task,
        attempt=2,
        model="model-b",
        at=START + timedelta(hours=2),
        pin={
            "model": "model-b",
            "escalated_from": "model-a",
            "escalation_reason": "fixture",
        },
    )
    rows.run(task, node="correct_1", model="model-b")
    rows.run(task, node="correct_1", attempt=2, model="model-b")
    rows.run(task, node="review_1", model="reviewer")
    report = _report(session)
    row = _state(report, "positives")
    assert row["initiating_model"] == "model-a"
    assert row["fixup_rounds_total"] == row["fixup_rounds_mean"] == 1
    assert (
        row["escalation_numerator"],
        row["escalation_denominator"],
        row["escalation_rate"],
    ) == (1, 1, 1)
    contributions = {(r["role"], r["model"]): r for r in report[SECTIONS[1]]}
    assert set(contributions) == {
        ("implement", "model-a"),
        ("implement", "model-b"),
        ("correct", "model-b"),
        ("review", "reviewer"),
    }
    for key, attempts, successes in [
        (("implement", "model-a"), 1, 0),
        (("implement", "model-b"), 1, 1),
        (("correct", "model-b"), 2, 2),
        (("review", "reviewer"), 1, 1),
    ]:
        contribution = contributions[key]
        assert contribution["distinct_tasks"] == 1
        assert (
            contribution["attempts"]
            == contribution["attempt_success_denominator"]
            == attempts
        )
        assert contribution["attempt_success_numerator"] == successes
        assert contribution["attempt_success_unknown_count"] == 0
        assert contribution["minutes"] == attempts * 60


@pytest.mark.parametrize(
    "signal",
    [
        "none",
        "run",
        "receipt",
        "nonwork_switch",
        "reviewer_fallback",
        "planned_split",
        "unknown_model",
        "late_receipt",
    ],
)
def test_documented_escalation_signals_and_denominator(session, rows, signal):
    task = rows.task(
        escalation='{"reason":"fixture"}'
        if signal in ("receipt", "late_receipt")
        else "{}",
        updated=AS_OF if signal == "late_receipt" else START,
    )
    rows.run(
        task,
        status="escalated" if signal == "run" else "succeeded",
        model=None if signal == "unknown_model" else "model-a",
    )
    if signal == "nonwork_switch":
        for node in ("planner", "conductor", "conductor_funding"):
            rows.run(task, node=node)
            rows.run(task, node=node, attempt=2, model="model-b")
    if signal == "reviewer_fallback":
        # Quota-driven reviewer substitution without a pool-escalation marker.
        rows.run(task, node="review_1", model="reviewer-a")
        rows.run(task, node="review_2", model="reviewer-b")
    if signal == "planned_split":
        # Planner-chosen per-node models without a pool-escalation marker.
        rows.run(task, node="implement_api", model="model-a")
        rows.run(task, node="implement_docs", model="model-b")
    row = _single(_report(session))
    observed = int(signal in ("run", "receipt"))
    unknown = int(signal in ("unknown_model", "late_receipt"))
    assert row["escalation_numerator"] == observed
    assert row["escalation_denominator"] == 1 - unknown
    assert row["escalation_unknown_count"] == unknown
    assert row["escalation_rate"] == (None if unknown else observed)


def test_duplicate_links_and_landing_audits_do_not_multiply(session, rows):
    task = rows.task()
    session_id = rows.session_row(task, named=True)
    session.execute(
        text("UPDATE swarm.swarm_task SET session_id = :session WHERE id = :task"),
        {"session": session_id, "task": task},
    )
    rows.run(task, session_id=session_id)
    rows.run(task, node="review_1", session_id=session_id)
    rows.start(task, session_id=session_id)
    rows.turn(session_id, cost=2)
    rows.turn(session_id, cost=5, seq=2)
    for _ in range(3):
        rows.merge(task)
        rows.audit(task, "merge_armed", head_sha=HEAD, pr_number=123)
        rows.audit(task, "merge_ejected", head_sha=HEAD, pr_number=123)
    report = _report(session)
    row = _state(report, "positives")
    assert (row["list_usd"], row["settled_usd"], row["ledger_upper_usd"]) == (7, 3, 3)
    assert row["usd_per_positive_lower"] == 7
    assert row["usd_per_positive_upper"] == 3
    contributions = report[SECTIONS[1]]
    assert sum(r["attempts"] for r in contributions) == 2
    assert sum(r["list_usd"] for r in contributions) == 7
    assert sum(r["settled_usd"] for r in contributions) == 3
    implement = next(r for r in contributions if r["role"] == "implement")
    assert implement["list_usd"] == 7
    review = next(r for r in contributions if r["role"] == "review")
    assert review["list_usd"] == 0
    assert _single(report, 3)["unpriced_turns"] == 0


def test_receipt_cohort_includes_later_attempts_until_as_of(session, rows):
    task = rows.task()
    rows.run(task, at=END + timedelta(hours=1))
    rows.run(task, attempt=2, model="future", at=AS_OF)
    rows.run(task, attempt=3, model="future", at=AS_OF + timedelta(seconds=1))
    session_id = rows.session_row(task)
    rows.start(task, session_id=session_id)
    rows.start(task, key="future_start", at=AS_OF, updated=AS_OF, maximum=100)
    rows.turn(session_id, at=END + timedelta(hours=1))
    rows.turn(session_id, at=AS_OF, seq=2, cost=100)
    for created in (START - timedelta(seconds=1), END):
        outside = rows.task(created=created, task_class="outside")
        rows.run(outside, at=START + timedelta(hours=1))
        outside_session = rows.session_row(outside, named=True)
        rows.turn(outside_session, cost=100)
    report = _report(session)
    row = _state(report, "censored")
    assert row["initiating_model"] == "model-a"
    assert row["list_usd"] == 2
    assert row["settled_usd"] == 3
    assert row["exposure_usd"] == 0
    assert sum(r["attempts"] for r in report[SECTIONS[1]]) == 1
    assert {r["model"] for r in report[SECTIONS[1]]} == {"model-a"}


def test_zero_positives_returns_null_ratios(session, rows):
    task = rows.task(settled=START + timedelta(hours=3))
    session_id = rows.session_row(task, named=True)
    rows.turn(session_id)
    rows.start(task)
    report = _report(session)
    _state(report, "failed")
    for row in [*report[SECTIONS[0]], *report[SECTIONS[2]]]:
        assert row["usd_per_positive_lower"] is None
        assert row["usd_per_positive_upper"] is None
        assert row["hours_per_positive"] is None


def test_elapsed_uses_terminal_times_and_includes_failed_cost(session, rows):
    rows.merge(rows.task())
    failed = rows.task(settled=START + timedelta(hours=5))
    rows.task()
    rows.task(settled=AS_OF)  # Future settlement remains censored at the cutoff.
    session_id = rows.session_row(failed, named=True)
    rows.turn(session_id, cost=7)
    rows.start(failed, status="failed", cost=4)
    report = _report(session)
    row = _single(report)
    assert (row["tasks"], row["positives"], row["failed"], row["censored"]) == (
        4,
        1,
        1,
        2,
    )
    assert row["hours_per_positive"] == 53  # 48 to merge plus 5 to failure.
    assert row["p50_hours_to_merge"] == 48
    assert row["usd_per_positive_lower"] == 7
    assert row["usd_per_positive_upper"] == 4
    assert _single(report, 3)["censored_tasks"] == 2


def test_missing_metadata_and_unmerged_tasks_have_unknown_bands(session, rows):
    rows.task()
    rows.merge(rows.task())  # No merged_prs snapshot and no stored issue labels.
    report = _report(session)
    bands = {(r["dimension"], r["band"]): r["tasks"] for r in report[SECTIONS[2]]}
    assert bands == {
        ("diff_size", "unknown"): 2,
        ("changed_files", "unknown"): 2,
        ("labels", "unknown"): 2,
        ("turn_count", "unknown"): 1,
        ("turn_count", "S"): 1,
    }
    coverage = _single(report, 3)
    assert (
        coverage["missing_size_metadata"]
        == coverage["missing_file_metadata"]
        == coverage["missing_label_metadata"]
        == 2
    )


def test_metadata_is_scoped_to_the_homelab_repository(session, rows):
    # merged_prs is keyed by number only and filled from homelab, so a receipt
    # for another repo whose merge audit names a colliding PR number is unknown.
    rows.merge(rows.task(repo="jomcgi-org/other"), pr=123)
    rows.metadata(pr=123, lines=400, files=8)
    report = _report(session)
    bands = {(r["dimension"], r["band"]): r["tasks"] for r in report[SECTIONS[2]]}
    assert bands[("diff_size", "unknown")] == 1
    assert bands[("changed_files", "unknown")] == 1
    assert ("diff_size", "L") not in bands
    assert ("changed_files", "L") not in bands
    coverage = _single(report, 3)
    assert coverage["missing_size_metadata"] == coverage["missing_file_metadata"] == 1


@pytest.mark.parametrize(
    "lines,size,files,file_band",
    [(49, "S", 1, "S"), (50, "M", 2, "M"), (299, "M", 5, "M"), (300, "L", 6, "L")],
)
def test_size_and_file_band_boundaries(session, rows, lines, size, files, file_band):
    rows.merge(rows.task())
    rows.metadata(lines=lines, files=files)
    report = _report(session)
    bands = {r["dimension"]: r for r in report[SECTIONS[2]]}
    assert bands["diff_size"]["band"] == size
    assert bands["changed_files"]["band"] == file_band
    assert bands["labels"]["band"] == "unknown"
    assert bands["diff_size"]["tasks"] == bands["diff_size"]["positives"] == 1
    assert _single(report, 3)["missing_size_metadata"] == 0


@pytest.mark.parametrize(
    "labels,updated,expected",
    [
        (["z", "a", "a"], START, '["a", "z"]'),
        ([], START, "[]"),
        (["a", 1], START, "unknown"),
        ({"a": True}, START, "unknown"),
        (["a"], AS_OF, "unknown"),
    ],
)
def test_label_snapshots_distinguish_known_empty_from_unknown(
    session, rows, labels, updated, expected
):
    task = rows.task()
    rows.merge(task)
    rows.labels(task, labels, updated=updated)
    report = _report(session)
    band = next(r for r in report[SECTIONS[2]] if r["dimension"] == "labels")
    assert band["band"] == expected
    assert _single(report, 3)["missing_label_metadata"] == int(expected == "unknown")


@pytest.mark.parametrize(
    "turn_count,expected", [(9, "S"), (10, "M"), (49, "M"), (50, "L")]
)
def test_turn_bands_include_unpriced_turns(session, rows, turn_count, expected):
    task = rows.task()
    rows.merge(task)
    session_id = rows.session_row(task, named=True)
    for seq in range(1, turn_count + 1):
        rows.turn(session_id, cost=None if seq == turn_count else 1, seq=seq)
    report = _report(session)
    band = next(r for r in report[SECTIONS[2]] if r["dimension"] == "turn_count")
    assert band["band"] == expected
    assert band["unpriced_turns"] == 1
    assert band["list_usd"] == turn_count - 1


def test_unpriced_turns_and_reservations_are_separate_cost_bounds(session, rows):
    task = rows.task()
    rows.merge(task)
    session_id = rows.session_row(task, named=True)
    rows.run(task, session_id=session_id)
    rows.turn(session_id, cost=None)
    rows.turn(session_id, cost=2, seq=2)
    rows.start(task, session_id=session_id, status="reserved", cost=None, maximum=10)
    rows.start(task, key="settled", status="failed", cost=3)
    rows.start(task, key="uncertain", status="uncertain", cost=None, maximum=5)
    rows.start(task, key="future_settlement", cost=100, maximum=7, updated=AS_OF)
    # Unknown usage on a terminal start keeps its reservation in the bound.
    rows.start(task, key="missing_cost", cost=None)
    # A proven-free basis commits nothing and is not a missing cost.
    rows.start(
        task, key="free_denial", status="failed", cost=None, basis="capacity_denied"
    )
    report = _report(session)
    row = _state(report, "positives")
    assert (
        row["list_usd"],
        row["unpriced_turns"],
        row["settled_usd"],
        row["exposure_usd"],
        row["ledger_upper_usd"],
    ) == (2, 1, 3, 32, 35)
    assert row["usd_per_positive_lower"] == 2
    assert row["usd_per_positive_upper"] == 35
    coverage = _single(report, 3)
    assert (
        coverage["unpriced_turns"],
        coverage["unsettled_reservations"],
        coverage["exposure_usd"],
        coverage["missing_settled_costs"],
    ) == (1, 3, 32, 1)
    for key, expected in [
        ("list_usd", 2),
        ("unpriced_turns", 1),
        ("settled_usd", 3),
        ("exposure_usd", 32),
        ("ledger_upper_usd", 35),
    ]:
        assert sum(r[key] for r in report[SECTIONS[1]]) == expected
        if key != "settled_usd":  # Difficulty rows expose the combined ledger bound.
            assert all(r[key] == expected for r in report[SECTIONS[2]])


@pytest.mark.parametrize("ejections", [0, 3])
def test_first_pass_ci_is_unknown_even_after_success(session, rows, ejections):
    task = rows.task()
    rows.merge(task)
    for _ in range(ejections):
        rows.audit(task, "merge_ejected", pr_number=123, head_sha=HEAD)
    row = _state(_report(session), "positives")
    assert row["first_pass_ci_numerator"] == row["first_pass_ci_denominator"] == 0
    assert row["first_pass_ci_unknown_count"] == 1
    assert row["first_pass_ci_rate"] is None
    assert _single(_report(session), 3)["first_pass_ci_unknown_count"] == 1
