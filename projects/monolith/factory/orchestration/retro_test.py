from __future__ import annotations

import json
import sqlite3
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


def _experiment_context(**overrides):
    boundary = NOW - retro.timedelta(hours=72)
    context = {
        "experiment": {
            "issue": 6782,
            "pr": 6788,
            "app": "monolith",
            "metric": "dispatch_failure",
            "minimum_attempts": 10,
        },
        "merged_at": (boundary - retro.timedelta(hours=1)).isoformat(),
        "deployment": {
            "note_id": "deployment-example",
            "observed_at": boundary.isoformat(),
            "revision": "0.1.0",
        },
        "rollout": {"verdict": "verified"},
        "baseline": {"attempts": 10, "dispatch_failure": 4, "unknown": 0},
        "post": {"attempts": 10, "dispatch_failure": 2, "unknown": 0},
    }
    context.update(overrides)
    return context


def test_optimizer_waits_for_rollout_and_strict_post_window():
    assert (
        retro.optimizer_evidence(_experiment_context(deployment=None), NOW)["status"]
        == "WAIT"
    )
    assert (
        retro.optimizer_evidence(
            _experiment_context(rollout={"verdict": "in_progress"}), NOW
        )["status"]
        == "WAIT"
    )
    deployment = {"observed_at": (NOW - retro.timedelta(hours=71)).isoformat()}
    assert (
        retro.optimizer_evidence(_experiment_context(deployment=deployment), NOW)[
            "status"
        ]
        == "WAIT"
    )


def test_optimizer_keeps_missing_traffic_and_coverage_unknown():
    assert (
        retro.optimizer_evidence(_experiment_context(post={"attempts": 0}), NOW)[
            "status"
        ]
        == "UNKNOWN"
    )
    assert (
        retro.optimizer_evidence(
            _experiment_context(post={"attempts": 10, "unknown": 1}), NOW
        )["status"]
        == "UNKNOWN"
    )
    assert (
        retro.optimizer_evidence(
            _experiment_context(overlapping_revisions=["0.2.0"]), NOW
        )["status"]
        == "UNKNOWN"
    )
    context = _experiment_context()
    context["experiment"].pop("minimum_attempts")
    assert retro.optimizer_evidence(context, NOW)["status"] == "UNKNOWN"


def test_optimizer_reports_observed_acceptance_or_regression_with_counts():
    accepted = retro.optimizer_evidence(_experiment_context(), NOW)
    assert accepted["status"] == "ACCEPTED"
    assert accepted["rates"] == {
        "baseline": 0.4,
        "post": 0.2,
        "denominator": "attempts",
    }
    assert "no causal or mature-outcome claim" in accepted["reason"]
    regressed = retro.optimizer_evidence(
        _experiment_context(post={"attempts": 10, "dispatch_failure": 5, "unknown": 0}),
        NOW,
    )
    assert regressed["status"] == "REGRESSED"


def test_optimizer_loader_bounds_windows_at_proven_deployment(monkeypatch):
    boundary = NOW - retro.timedelta(hours=72)
    experiment = {
        "issue": 6782,
        "pr": 6788,
        "app": "monolith",
        "expected_revision": "0.1.0",
        "writeback_commit": "a" * 40,
    }
    monkeypatch.setattr(
        retro,
        "_optimizer_github",
        lambda: {
            "experiment": experiment,
            "merged_at": (boundary - retro.timedelta(hours=1)).isoformat(),
            "contains_merge": True,
        },
    )
    monkeypatch.setattr(
        retro, "_verify_optimizer_rollout", lambda *_: {"verdict": "verified"}
    )
    observed = []

    def rows(_session, sql, params):
        observed.append((sql, params))
        if sql in (retro._OPTIMIZER_BOUNDARY, retro._OPTIMIZER_OBSERVATIONS):
            return [
                {
                    "note_id": "deployment-example",
                    "observed_at": boundary,
                    "status": "complete",
                    "extra": {
                        "status": "complete",
                        "deployed_revision": "0.1.0",
                        "requested_revision": "0.1.0",
                        "newest_freight_version": "0.1.0",
                        "writeback_commit": "a" * 40,
                    },
                }
            ]
        if sql == retro._OPTIMIZER_COUNTS:
            return {"baseline": [{"attempts": 10}], "post": [{"attempts": 10}]}[
                "baseline" if params["end"] == boundary else "post"
            ]
        return [{"unknown_reservation_usd": 12.0, "settled_cost_usd": 3.0}]

    monkeypatch.setattr(retro, "_rows", rows)
    loaded = retro._load_optimizer(object(), NOW)
    assert loaded["baseline"]["end"] == loaded["post"]["start"] == boundary.isoformat()
    assert loaded["post"]["end"] == NOW.isoformat()
    assert loaded["post"]["unknown_reservation_usd"] == 12
    assert loaded["post"]["settled_cost_usd"] == 3
    for sql, params in observed:
        if sql == retro._OPTIMIZER_COUNTS:
            assert params["end"] - params["start"] == retro.timedelta(hours=72)
            assert params["as_of"] == NOW


def test_optimizer_merge_without_provenance_never_loads_windows(monkeypatch):
    monkeypatch.setattr(
        retro,
        "_optimizer_github",
        lambda: {"merged_at": NOW.isoformat(), "contains_merge": False},
    )
    monkeypatch.setattr(
        retro,
        "_rows",
        lambda *_: (_ for _ in ()).throw(AssertionError("unproven window")),
    )
    loaded = retro._load_optimizer(object(), NOW)
    assert retro.optimizer_evidence(loaded, NOW)["status"] == "WAIT"


def test_optimizer_loader_holds_saturated_history(monkeypatch):
    monkeypatch.setattr(
        retro,
        "_optimizer_github",
        lambda: {
            "experiment": {"app": "monolith", "expected_revision": "0.1.0"},
            "merged_at": NOW.isoformat(),
            "contains_merge": True,
        },
    )
    monkeypatch.setattr(
        retro, "_verify_optimizer_rollout", lambda *_: {"verdict": "verified"}
    )

    def rows(_session, sql, params):
        if sql == retro._OPTIMIZER_BOUNDARY:
            return [
                {"note_id": "deployed", "observed_at": NOW - retro.timedelta(hours=72)}
            ]
        assert params["limit"] == retro._OPTIMIZER_OBSERVATION_LIMIT + 1
        return [{}] * (retro._OPTIMIZER_OBSERVATION_LIMIT + 1)

    monkeypatch.setattr(retro, "_rows", rows)
    loaded = retro._load_optimizer(object(), NOW)
    assert retro.optimizer_evidence(loaded, NOW)["status"] == "UNKNOWN"
    assert "saturated" in loaded["error"]


def test_optimizer_loader_ignores_deployments_after_completed_window(monkeypatch):
    boundary = NOW - retro.timedelta(hours=74)
    experiment = {
        "app": "monolith",
        "expected_revision": "0.1.0",
        "writeback_commit": "a" * 40,
    }
    monkeypatch.setattr(
        retro,
        "_optimizer_github",
        lambda: {
            "experiment": experiment,
            "merged_at": (boundary - retro.timedelta(hours=1)).isoformat(),
            "contains_merge": True,
        },
    )
    monkeypatch.setattr(
        retro, "_verify_optimizer_rollout", lambda *_: {"verdict": "verified"}
    )

    def rows(_session, sql, _params):
        if sql in (retro._OPTIMIZER_BOUNDARY, retro._OPTIMIZER_OBSERVATIONS):
            return [
                {
                    "note_id": "deployed",
                    "observed_at": boundary,
                    "status": "complete",
                    "extra": {
                        "deployed_revision": "0.1.0",
                        "requested_revision": "0.1.0",
                        "newest_freight_version": "0.1.0",
                        "writeback_commit": "a" * 40,
                    },
                },
                {
                    "observed_at": boundary + retro.timedelta(hours=73),
                    "extra": {"deployed_revision": "0.2.0"},
                },
            ]
        return [{"attempts": 10}]

    monkeypatch.setattr(retro, "_rows", rows)
    loaded = retro._load_optimizer(object(), NOW)
    assert loaded["overlapping_revisions"] == []
    assert loaded["post"]["end"] == (boundary + retro.timedelta(hours=72)).isoformat()


def test_optimizer_missing_boundary_does_not_scan_poll_history(monkeypatch):
    monkeypatch.setattr(
        retro,
        "_optimizer_github",
        lambda: {
            "experiment": {"app": "monolith", "expected_revision": "0.1.0"},
            "merged_at": (NOW - retro.timedelta(days=30)).isoformat(),
            "contains_merge": True,
        },
    )
    monkeypatch.setattr(
        retro, "_verify_optimizer_rollout", lambda *_: {"verdict": "verified"}
    )

    def rows(_session, sql, params):
        assert sql == retro._OPTIMIZER_BOUNDARY
        return []

    monkeypatch.setattr(retro, "_rows", rows)
    loaded = retro._load_optimizer(object(), NOW)
    assert "post" not in loaded
    assert retro.optimizer_evidence(loaded, NOW)["status"] == "WAIT"


def test_optimizer_old_window_coverage_is_independent_of_later_polls(monkeypatch):
    boundary = NOW - retro.timedelta(days=20)
    end = boundary + retro.timedelta(hours=72)
    monkeypatch.setattr(
        retro,
        "_optimizer_github",
        lambda: {
            "experiment": {
                "app": "monolith",
                "expected_revision": "0.1.0",
                "writeback_commit": "a" * 40,
            },
            "merged_at": (boundary - retro.timedelta(days=1)).isoformat(),
            "contains_merge": True,
        },
    )
    monkeypatch.setattr(
        retro, "_verify_optimizer_rollout", lambda *_: {"verdict": "verified"}
    )

    def rows(_session, sql, params):
        if sql == retro._OPTIMIZER_BOUNDARY:
            assert params["revision"] == "0.1.0" and params["writeback"] == "a" * 40
            return [{"note_id": "deployed", "observed_at": boundary}]
        if sql == retro._OPTIMIZER_OBSERVATIONS:
            assert params["since"] == boundary and params["as_of"] == end
            # Exactly the limit is complete. The extra sentinel detects overflow.
            return [
                {"observed_at": boundary, "extra": {"deployed_revision": "0.2.0"}}
            ] * 5000
        return [{"attempts": 10}]

    monkeypatch.setattr(retro, "_rows", rows)
    loaded = retro._load_optimizer(object(), NOW)
    assert loaded["observation_coverage"]["complete"] is True
    assert loaded["post"]["end"] == end.isoformat()
    assert loaded["overlapping_revisions"] == ["0.2.0"]
    assert retro.optimizer_evidence(loaded, NOW)["status"] == "UNKNOWN"


def test_optimizer_sql_selects_exact_boundary_before_bounded_history():
    # SQLite supports the JSON extraction and named bounds used by these two
    # SELECTs, so exercise the SQL rather than only mocking its result shape.
    with sqlite3.connect(":memory:") as db:
        db.execute("ATTACH DATABASE ':memory:' AS knowledge")
        db.execute("""CREATE TABLE knowledge.notes (
            note_id TEXT, observed_at TEXT, status TEXT, extra TEXT,
            source TEXT, scope TEXT, verification_state TEXT, deleted_at TEXT
        )""")
        boundary = NOW - retro.timedelta(days=20)
        merged = boundary - retro.timedelta(hours=1)
        end = boundary + retro.timedelta(hours=72)
        identity = {
            "app": "monolith",
            "newest_freight_version": "0.1.0",
            "requested_revision": "0.1.0",
            "deployed_revision": "0.1.0",
            "writeback_commit": "a" * 40,
        }

        def insert(note_id, observed_at, extra, verified="verified"):
            db.execute(
                "INSERT INTO knowledge.notes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    note_id,
                    observed_at.isoformat(),
                    "complete",
                    json.dumps(extra),
                    "deployment-observation",
                    "environment:homelab",
                    verified,
                    None,
                ),
            )

        insert("wrong-writeback", merged, {**identity, "writeback_commit": "b" * 40})
        insert("unverified", merged, identity, "unverified")
        insert("deployed", boundary, identity)
        insert(
            "overlap",
            boundary + retro.timedelta(hours=1),
            {**identity, "deployed_revision": "0.2.0"},
        )
        for index in range(5001):
            insert(f"later-{index}", end, identity)
        params = {
            "since": merged.isoformat(),
            "as_of": NOW.isoformat(),
            "app": "monolith",
            "revision": "0.1.0",
            "writeback": "a" * 40,
        }
        rows = db.execute(retro._OPTIMIZER_BOUNDARY, params).fetchall()
        assert [row[0] for row in rows] == ["deployed"]
        rows = db.execute(
            retro._OPTIMIZER_OBSERVATIONS,
            {
                "since": boundary.isoformat(),
                "as_of": end.isoformat(),
                "app": "monolith",
                "limit": retro._OPTIMIZER_OBSERVATION_LIMIT + 1,
            },
        ).fetchall()
        assert [row[0] for row in rows] == ["deployed", "overlap"]
# Learning deliberately stays inside this existing workflow/test target.
import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine
from factory.orchestration import learning


@pytest.fixture
def learning_db(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_LEARNING_HANDOFF_ENABLED", "true")
    engine = create_engine(f"sqlite:///{tmp_path / 'learning.db'}")
    with Session(engine) as db:
        db.execute(
            text(
                "CREATE TABLE routine_jobs (name TEXT PRIMARY KEY, routine_kind TEXT, interval_secs INTEGER, next_run_at TEXT, locked_by TEXT, last_status TEXT, payload TEXT, created_by TEXT)"
            )
        )
        db.execute(
            text(
                "CREATE TABLE agent_sessions (id INTEGER PRIMARY KEY, repo TEXT, model TEXT, local_session_id TEXT, workflow_id TEXT, node_key TEXT, status TEXT)"
            )
        )
        db.execute(
            text(
                "CREATE TABLE agent_turns (session_id INTEGER, seq INTEGER, result_text TEXT, terminal_reason TEXT, stop_reason TEXT, created_at TEXT)"
            )
        )
        payload = {
            "model": "luna",
            "repo": learning.REPO,
            "learning_stage": "evidence",
            "digest": "factory-retro",
        }
        db.execute(
            text(
                "INSERT INTO routine_jobs (name,payload,interval_secs) VALUES ('factory-retro-daily',:payload,86400)"
            ),
            {"payload": json.dumps(payload)},
        )
        for sid, model, local, workflow in [
            (1, "luna", "w:qwen-drain:factory-retro-daily", "w"),
            (2, "astra", "factory:source:implement:1", "source"),
        ]:
            db.execute(
                text(
                    "INSERT INTO agent_sessions VALUES (:sid,:repo,:model,:local,:workflow,'qwen-drain','completed')"
                ),
                dict(
                    sid=sid,
                    repo=learning.REPO,
                    model=model,
                    local=local,
                    workflow=workflow,
                ),
            )
            db.execute(
                text(
                    "INSERT INTO agent_turns VALUES (:sid,1,:result,'completed',NULL,:now)"
                ),
                dict(
                    sid=sid,
                    result="original evidence",
                    now=datetime.now(timezone.utc).isoformat(),
                ),
            )
        db.commit()
        yield db


def _finding():
    return {
        "findings": [
            {
                "signature": "source load fails",
                "code_sha": "a" * 40,
                "hypothesis": "bounded failure",
                "sources": [{"session_id": 2, "turn_seq": 1}],
            }
        ]
    }


def _learn(db, output):
    db.execute(
        text("UPDATE agent_turns SET result_text=:result WHERE session_id=1"),
        {"result": json.dumps(output)},
    )
    learning.complete(db, "factory-retro-daily", "luna-drainer:w:0")


def _learning_jobs(db):
    return db.execute(
        text(
            "SELECT name,payload FROM routine_jobs WHERE name LIKE 'factory-learning:%'"
        )
    ).all()


def test_learning_pins_evidence_and_never_rearms_duplicate(learning_db):
    db = learning_db
    _learn(db, _finding())
    job = _learning_jobs(db)[0]
    payload = json.loads(job.payload)
    assert payload["model"] == "astra"
    assert payload["learning_evidence"]["origin"]["session_id"] == 1
    assert payload["learning_evidence"]["sources"][0]["result_sha256"] == learning._sha(
        "original evidence"
    )
    db.execute(
        text(
            "UPDATE routine_jobs SET next_run_at=NULL,last_status='ok' WHERE name=:name"
        ),
        {"name": job.name},
    )
    _learn(db, _finding())
    assert len(_learning_jobs(db)) == 1
    assert (
        db.execute(
            text("SELECT next_run_at FROM routine_jobs WHERE name=:name"),
            {"name": job.name},
        ).scalar()
        is None
    )
    parent = json.loads(
        db.execute(
            text("SELECT payload FROM routine_jobs WHERE name='factory-retro-daily'")
        ).scalar()
    )
    assert parent["learning_last_evidence"]["disposition"] == "duplicate"


def test_learning_pending_blocks_new_fingerprint(learning_db):
    db = learning_db
    _learn(db, _finding())
    output = _finding()
    db.execute(
        text(
            "UPDATE agent_turns SET result_text='new original evidence' WHERE session_id=2"
        )
    )
    _learn(db, output)
    assert len(_learning_jobs(db)) == 1
    parent = json.loads(
        db.execute(
            text("SELECT payload FROM routine_jobs WHERE name='factory-retro-daily'")
        ).scalar()
    )
    assert parent["learning_last_evidence"]["disposition"] == "evaluation_pending"


@pytest.mark.parametrize(
    "change", ["missing", "self", "stale", "foreign", "incomplete", "oversized"]
)
def test_learning_rejects_bad_evidence(learning_db, change):
    db = learning_db
    output = _finding()
    if change == "missing":
        output["findings"][0]["sources"][0]["session_id"] = 99
    if change == "self":
        output["findings"][0]["sources"][0]["session_id"] = 1
    if change == "stale":
        db.execute(
            text("UPDATE agent_turns SET created_at='2000-01-01' WHERE session_id=2")
        )
    if change == "foreign":
        db.execute(text("UPDATE agent_sessions SET repo='other/repo' WHERE id=2"))
    if change == "incomplete":
        db.execute(
            text("UPDATE agent_turns SET terminal_reason='error' WHERE session_id=2")
        )
    if change == "oversized":
        output["findings"][0]["hypothesis"] = "x" * 8001
    with pytest.raises(ValueError):
        _learn(db, output)
    assert _learning_jobs(db) == []


def test_learning_disabled_and_wrong_owner_fail_closed(learning_db, monkeypatch):
    with pytest.raises(ValueError, match="owner"):
        learning.complete(learning_db, "factory-retro-daily", "wrong")
    monkeypatch.delenv("FACTORY_LEARNING_HANDOFF_ENABLED")
    with pytest.raises(ValueError, match="disabled"):
        _learn(learning_db, _finding())
    assert _learning_jobs(learning_db) == []


def test_learning_retains_evaluation_lineage_without_authorizing_links(learning_db):
    db = learning_db
    _learn(db, _finding())
    job = _learning_jobs(db)[0]
    db.execute(
        text(
            "INSERT INTO agent_sessions VALUES (3,:repo,'astra',:local,'eval','qwen-drain','completed')"
        ),
        {"repo": learning.REPO, "local": f"eval:qwen-drain:{job.name}"},
    )
    result = {
        "verdict": "duplicate",
        "issue_number": 6781,
        "pr_number": None,
        "rationale": "existing owner",
    }
    db.execute(
        text("INSERT INTO agent_turns VALUES (3,1,:result,'completed',NULL,:now)"),
        {"result": json.dumps(result), "now": datetime.now(timezone.utc).isoformat()},
    )
    learning.complete(db, job.name, "luna-drainer:eval:0")
    payload = json.loads(_learning_jobs(db)[0].payload)
    assert payload["learning_evidence"]["origin"]["session_id"] == 1
    assert payload["learning_evaluation"]["session_id"] == 3
    assert payload["learning_evaluation"]["links_verified_by_server"] is False
    assert len(_learning_jobs(db)) == 1


def test_learning_rewording_is_not_new_evidence(learning_db):
    db = learning_db
    _learn(db, _finding())
    job = _learning_jobs(db)[0]
    db.execute(
        text(
            "UPDATE routine_jobs SET next_run_at=NULL,last_status='ok' WHERE name=:name"
        ),
        {"name": job.name},
    )
    output = _finding()
    output["findings"][0]["code_sha"] = "b" * 40
    output["findings"][0]["signature"] = "different wording"
    _learn(db, output)
    assert len(_learning_jobs(db)) == 1


def test_learning_unknown_evaluation_holds_new_evidence(learning_db):
    db = learning_db
    _learn(db, _finding())
    job = _learning_jobs(db)[0]
    payload = json.loads(job.payload)
    payload["learning_evaluation"] = {"verdict": "unknown"}
    db.execute(
        text(
            "UPDATE routine_jobs SET next_run_at=NULL,last_status='ok',payload=:payload WHERE name=:name"
        ),
        {"name": job.name, "payload": json.dumps(payload)},
    )
    db.execute(
        text(
            "UPDATE agent_turns SET result_text='new original evidence' WHERE session_id=2"
        )
    )
    _learn(db, _finding())
    assert len(_learning_jobs(db)) == 1


def test_learning_prompt_contains_bounded_original_evidence(learning_db):
    db = learning_db
    db.execute(
        text("UPDATE agent_turns SET result_text=:result WHERE session_id=2"),
        {"result": "original failure " + "x" * 3000},
    )
    _learn(db, _finding())
    payload = json.loads(_learning_jobs(db)[0].payload)
    source = payload["learning_evidence"]["sources"][0]
    assert source["truncated"] is True
    assert len(source["excerpt"]) == 2000
    assert "original failure" in learning.prompt(payload)
    assert "omitted" in learning.prompt(payload)
