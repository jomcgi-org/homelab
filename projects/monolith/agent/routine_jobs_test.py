"""Hermetic tests for routine job updates."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine

from agent import routine_jobs


@pytest.fixture
def freshness_engine(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'freshness.db'}")
    with Session(engine) as session:
        session.execute(
            text("""
            CREATE TABLE routine_jobs (
                name TEXT PRIMARY KEY, routine_kind TEXT, interval_secs INTEGER,
                next_run_at TEXT, last_run_at TEXT, last_status TEXT,
                last_summary TEXT, locked_by TEXT, locked_at TEXT,
                ttl_secs INTEGER, payload TEXT, created_by TEXT, created_at TEXT
            )
        """)
        )
        session.execute(
            text("""
            CREATE TABLE raw_inputs (raw_id TEXT PRIMARY KEY, source TEXT)
        """)
        )
        session.commit()
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    return engine


def _queue_freshness_job(engine, name, *, source=None, **overrides):
    values = {
        "name": name,
        "kind": "kg-drain",
        "due": "2026-01-01 00:00:00",
        "status": None,
        "locked_by": None,
        "locked_at": None,
        "payload": json.dumps({"raw_id": name}),
    } | overrides
    with Session(engine) as session:
        session.execute(
            text("""
            INSERT INTO routine_jobs
                (name, routine_kind, next_run_at, last_status,
                 locked_by, locked_at, ttl_secs, payload)
            VALUES (:name, :kind, :due, :status, :locked_by, :locked_at, 60, :payload)
        """),
            values,
        )
        if source:
            session.execute(
                text("INSERT INTO raw_inputs VALUES (:name, :source)"),
                {"name": name, "source": source},
            )
        session.commit()


def _freshness_claim():
    return routine_jobs.claim_job(
        "luna-drainer",
        60,
        kinds=["kg-drain", "qwen-drain"],
        prefer_repo_freshness=True,
    )


def test_freshness_prefers_due_scout_then_preserves_fifo_work(freshness_engine):
    _queue_freshness_job(freshness_engine, "old-a")
    _queue_freshness_job(freshness_engine, "old-b", kind="qwen-drain")
    _queue_freshness_job(freshness_engine, "kg-repo-diff", due="2026-01-02 00:00:00")
    assert _freshness_claim()["name"] == "kg-repo-diff"
    assert routine_jobs.complete_job("kg-repo-diff", "ok")
    _queue_freshness_job(
        freshness_engine, "new-diff", source="repo-diff", due="2026-01-03 00:00:00"
    )
    assert _freshness_claim()["name"] == "old-a"
    assert routine_jobs.complete_job("old-a", "ok")
    assert _freshness_claim()["name"] == "old-b"


def test_freshness_cooldown_persists_after_connection_restart(
    freshness_engine, monkeypatch
):
    _queue_freshness_job(freshness_engine, "kg-repo-diff")
    assert _freshness_claim()["name"] == "kg-repo-diff"
    assert routine_jobs.complete_job("kg-repo-diff", "ok")
    _queue_freshness_job(freshness_engine, "old")
    _queue_freshness_job(freshness_engine, "derived", source="repo-diff")
    url = freshness_engine.url
    freshness_engine.dispose()
    restarted = create_engine(url)
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: restarted)
    with Session(restarted) as session:
        session.execute(
            text("""
            UPDATE routine_jobs SET last_run_at = datetime(CURRENT_TIMESTAMP, '-299 seconds')
             WHERE name = 'kg-repo-diff'
        """)
        )
        session.commit()
    assert _freshness_claim()["name"] == "old"
    assert routine_jobs.complete_job("old", "ok")
    _queue_freshness_job(restarted, "another-old")
    with Session(restarted) as session:
        session.execute(
            text("""
            UPDATE routine_jobs SET last_run_at = datetime(CURRENT_TIMESTAMP, '-301 seconds')
             WHERE name = 'kg-repo-diff'
        """)
        )
        session.commit()
    assert _freshness_claim()["name"] == "derived"
    assert routine_jobs.complete_job("derived", "ok")
    _queue_freshness_job(restarted, "newer-diff", source="repo-diff")
    assert _freshness_claim()["name"] == "another-old"


@pytest.mark.parametrize("blocked", ["future", "locked", "unknown", "wrong-kind"])
def test_freshness_never_bypasses_eligibility(freshness_engine, blocked):
    with Session(freshness_engine) as session:
        now = session.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one()
    overrides = {
        "future": {"due": "2999-01-01 00:00:00"},
        "locked": {"locked_by": "other", "locked_at": now},
        "unknown": {"status": routine_jobs.UNKNOWN_INVOCATION},
        "wrong-kind": {"kind": "different-lane"},
    }[blocked]
    _queue_freshness_job(freshness_engine, "kg-repo-diff", **overrides)
    _queue_freshness_job(freshness_engine, "ordinary")
    assert _freshness_claim()["name"] == "ordinary"
    assert _freshness_claim() is None


def test_freshness_classification_uses_persisted_raw_source(freshness_engine):
    _queue_freshness_job(freshness_engine, "old", source="agent-report")
    _queue_freshness_job(
        freshness_engine,
        "spoofed",
        source="agent-report",
        payload=json.dumps({"raw_id": "spoofed", "source": "repo-diff"}),
    )
    _queue_freshness_job(
        freshness_engine, "real-diff", source="repo-diff", due="2026-01-02 00:00:00"
    )
    assert _freshness_claim()["name"] == "real-diff"
    assert routine_jobs.complete_job("real-diff", "ok")
    assert _freshness_claim()["name"] == "old"


def test_freshness_uses_idle_slots_without_duplicate_claims(freshness_engine):
    _queue_freshness_job(freshness_engine, "diff-a", source="repo-diff")
    _queue_freshness_job(freshness_engine, "diff-b", source="repo-diff")
    assert _freshness_claim()["name"] == "diff-a"
    # The first lease is still live. A second claimant can take the other row,
    # even during cooldown, because no ordinary job needs the slot.
    assert _freshness_claim()["name"] == "diff-b"
    assert _freshness_claim() is None


def test_default_claim_stays_fifo_without_freshness_opt_in(freshness_engine):
    _queue_freshness_job(freshness_engine, "old")
    _queue_freshness_job(freshness_engine, "kg-repo-diff", due="2026-01-02 00:00:00")
    assert routine_jobs.claim_job("other", 60)["name"] == "old"


def _guarded_engine(tmp_path, name, *, locked_at, status=None, next_run_at=None):
    engine = create_engine(f"sqlite:///{tmp_path / (name.replace(':', '-') + '.db')}")
    with Session(engine) as session:
        session.execute(
            text(
                """
                CREATE TABLE routine_jobs (
                    name TEXT PRIMARY KEY, routine_kind TEXT, interval_secs INTEGER,
                    next_run_at TEXT, last_run_at TEXT, last_status TEXT,
                    last_summary TEXT, locked_by TEXT, locked_at TEXT,
                    ttl_secs INTEGER, payload TEXT, created_by TEXT, created_at TEXT
                )
                """
            )
        )
        session.execute(
            text(
                """
                INSERT INTO routine_jobs
                    (name, routine_kind, next_run_at, last_status, last_summary,
                     locked_by, locked_at, payload, created_by, created_at)
                VALUES
                    (:name, 'kg-drain', :next_run_at, :status, :summary,
                     :locked_by, :locked_at, :payload, 'factory', :created_at)
                """
            ),
            {
                "name": name,
                "next_run_at": next_run_at,
                "status": status,
                "summary": None,
                "locked_by": "luna-drainer" if locked_at is not None else None,
                "locked_at": locked_at,
                "payload": json.dumps({"prompt": "keep"}),
                "created_at": "2026-01-01 00:00:00",
            },
        )
        session.commit()
    return engine


def test_guarded_unknown_hold_matches_exact_claim(monkeypatch, tmp_path):
    engine = _guarded_engine(
        tmp_path, "kg:sha256", locked_at="2026-09-07 00:00:00", next_run_at=None
    )
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    expected = datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert routine_jobs.hold_job_for_unknown_outcome(
        "kg:sha256",
        2797,
        "unknown",
        expected_locked_by="luna-drainer",
        expected_locked_at=expected.isoformat(),
    )
    with Session(engine) as session:
        row = session.execute(text("SELECT * FROM routine_jobs")).one()
    assert row.payload == json.dumps({"prompt": "keep"})
    assert row.created_by == "factory"
    assert row.name == "kg:sha256"
    assert row.last_status == routine_jobs.UNKNOWN_INVOCATION
    assert row.next_run_at is None
    assert row.locked_by is None
    assert row.locked_at is None


def test_guarded_unknown_hold_idempotency_is_unlocked(monkeypatch, tmp_path):
    engine = _guarded_engine(
        tmp_path,
        "kg:idempotent",
        locked_at=None,
        status=routine_jobs.UNKNOWN_INVOCATION,
        next_run_at=None,
    )
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    with Session(engine) as session:
        session.execute(
            text("UPDATE routine_jobs SET last_summary = :summary WHERE name = :name"),
            {"name": "kg:idempotent", "summary": "session_id=2797: original"},
        )
        session.commit()
    assert routine_jobs.hold_job_for_unknown_outcome(
        "kg:idempotent",
        2797,
        "replacement",
        expected_locked_by="luna-drainer",
        expected_locked_at="2026-09-07T00:00:01+00:00",
    )
    with Session(engine) as session:
        row = session.execute(text("SELECT * FROM routine_jobs")).one()
    assert row.last_summary == "session_id=2797: original"
    assert row.payload == json.dumps({"prompt": "keep"})
    assert row.created_by == "factory"


def test_guarded_unknown_hold_does_not_touch_successor(monkeypatch, tmp_path):
    engine = _guarded_engine(
        tmp_path,
        "kg:successor",
        locked_at="2026-09-07 00:00:02",
        status="running",
        next_run_at="2026-09-07 00:01:00",
    )
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    assert not routine_jobs.hold_job_for_unknown_outcome(
        "kg:successor",
        2797,
        "stale",
        expected_locked_by="luna-drainer",
        expected_locked_at="2026-09-07T00:00:00+00:00",
    )
    with Session(engine) as session:
        row = session.execute(text("SELECT * FROM routine_jobs")).one()
    assert (row.locked_by, row.locked_at, row.last_status, row.next_run_at) == (
        "luna-drainer",
        "2026-09-07 00:00:02",
        "running",
        "2026-09-07 00:01:00",
    )


def test_update_job_payload_replaces_only_payload(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'routine-jobs.db'}")
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    with Session(engine) as session:
        session.execute(
            text(
                """
                CREATE TABLE routine_jobs (
                    name TEXT PRIMARY KEY,
                    routine_kind TEXT NOT NULL,
                    interval_secs INTEGER,
                    payload TEXT,
                    locked_by TEXT
                )
                """
            )
        )
        session.execute(
            text(
                """
                INSERT INTO routine_jobs
                    (name, routine_kind, interval_secs, payload, locked_by)
                VALUES
                    ('scout', 'kg-drain', 3600, :payload, 'holder')
                """
            ),
            {"payload": json.dumps({"mode": "repo-diff", "last_sha": None})},
        )
        session.commit()

    assert routine_jobs.update_job_payload(
        "scout", {"mode": "repo-diff", "last_sha": "a" * 40}
    )

    with Session(engine) as session:
        row = session.execute(text("SELECT * FROM routine_jobs")).one()
    assert json.loads(row.payload) == {
        "mode": "repo-diff",
        "last_sha": "a" * 40,
    }
    assert row.routine_kind == "kg-drain"
    assert row.interval_secs == 3600
    assert row.locked_by == "holder"


def test_unknown_job_hold_survives_reclaim_and_preserves_recurring_payload(
    monkeypatch, tmp_path
):
    engine = create_engine(f"sqlite:///{tmp_path / 'held-jobs.db'}")
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    payload = json.dumps({"raw_id": "raw-retain", "attempts": 1})
    with Session(engine) as session:
        session.execute(
            text("""
            CREATE TABLE routine_jobs (
                name TEXT PRIMARY KEY, routine_kind TEXT, interval_secs INTEGER,
                next_run_at TEXT, last_run_at TEXT, last_status TEXT, last_summary TEXT,
                locked_by TEXT, locked_at TEXT, ttl_secs INTEGER, payload TEXT,
                created_by TEXT, created_at TEXT
            )
        """)
        )
        for name, interval in (
            ("one-shot", None),
            ("recurring", 3600),
            ("ordinary", 3600),
        ):
            session.execute(
                text("""
                INSERT INTO routine_jobs (name, routine_kind, interval_secs,
                    next_run_at, locked_by, locked_at, ttl_secs, payload)
                VALUES (:name, 'kg-drain', :interval, CURRENT_TIMESTAMP,
                    'dead-drainer', '2000-01-01', 1, :payload)
            """),
                {"name": name, "interval": interval, "payload": payload},
            )
        session.commit()
    assert routine_jobs.complete_job("ordinary", "ok", "finished") is True
    assert routine_jobs.trigger_job("ordinary") is True
    assert routine_jobs.claim_job("worker", 60, name="ordinary")["name"] == "ordinary"
    assert routine_jobs.defer_job("ordinary", 1) is True
    assert routine_jobs.deregister_job("ordinary") is True
    for name in ("one-shot", "recurring"):
        assert routine_jobs.hold_job_for_unknown_outcome(name, 2448, "reconcile first")
    for name in ("one-shot", "recurring"):
        assert routine_jobs.complete_job(name, "ok", "late result") is False
        assert routine_jobs.trigger_job(name) is False
        assert routine_jobs.defer_job(name, 1) is False
        assert routine_jobs.deregister_job(name) is False
    # Simulate later processes opening fresh connections, including an ordinary
    # re-arm. Neither periodic nor named admission may infer reconciliation.
    with Session(engine) as session:
        rows = session.execute(text("SELECT * FROM routine_jobs ORDER BY name")).all()
        assert all(row.next_run_at is None for row in rows)
        assert all(row.last_status == "invocation_outcome_unknown" for row in rows)
        assert all(row.payload == payload for row in rows)
        assert all("session_id=2448" in row.last_summary for row in rows)
        assert rows[1].interval_secs == 3600
        session.execute(text("UPDATE routine_jobs SET next_run_at = CURRENT_TIMESTAMP"))
        session.commit()
    assert routine_jobs.claim_job("replacement", 60, kinds=["kg-drain"]) is None
    assert routine_jobs.claim_job("replacement", 60, name="recurring") is None


def test_supplied_claim_session_rolls_back_lease_without_committing_other_work(
    freshness_engine,
):
    _queue_freshness_job(freshness_engine, "job")
    with Session(freshness_engine) as db:
        claimed = routine_jobs.claim_job("holder", 60, session=db)
        assert claimed["name"] == "job"
        db.rollback()
    assert _freshness_claim()["name"] == "job"


@pytest.fixture
def worker_database(freshness_engine, monkeypatch):
    from agent_sessions import admission

    def lock(db):
        db.execute(
            text(
                "UPDATE routine_jobs SET name=name WHERE routine_kind='_drainer-worker'"
            )
        )

    monkeypatch.setattr(admission, "lock_pool", lock)
    return freshness_engine


def test_parallel_worker_refill_reserves_exactly_two_intents(worker_database):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(2) as workers:
        results = list(
            workers.map(
                lambda _: routine_jobs.reserve_drainer_workers({}, {}, set()), range(2)
            )
        )
    assert sorted(0 if result is None else len(result) for result in results) == [0, 2]
    intents = routine_jobs.drainer_worker_intents()
    assert {row["workflow_id"] for row in intents.values()} == {
        "_drainer-worker:0:1",
        "_drainer-worker:1:1",
    }
    assert _freshness_claim() is None
    assert not routine_jobs.trigger_job("_drainer-worker:0")
    assert not routine_jobs.deregister_job("_drainer-worker:0")
    assert routine_jobs.drainer_worker_intents() == intents


def test_lost_enqueue_response_retries_same_ids_and_completion_refills_only_one(
    worker_database,
):
    first = routine_jobs.reserve_drainer_workers({}, {}, set())
    intents = routine_jobs.drainer_worker_intents()
    assert (
        routine_jobs.reserve_drainer_workers(intents, dict.fromkeys(first), set())
        == first
    )
    statuses = {first[0]: "SUCCESS", first[1]: "PENDING"}
    next_ids = routine_jobs.reserve_drainer_workers(intents, statuses, {first[1]})
    assert next_ids == ["_drainer-worker:0:2"]
    # A competing observer with the old snapshot cannot enqueue another successor.
    assert routine_jobs.reserve_drainer_workers(intents, statuses, {first[1]}) is None
    assert (
        routine_jobs.drainer_worker_intents()["_drainer-worker:1"]
        == intents["_drainer-worker:1"]
    )


def test_completing_worker_refills_without_waiting_for_other_worker(worker_database):
    ids = routine_jobs.reserve_drainer_workers({}, {}, set())
    intents = routine_jobs.drainer_worker_intents()
    assert routine_jobs.reserve_drainer_workers(
        intents,
        dict.fromkeys(ids, "PENDING"),
        set(ids),
        completing_workflow_id=ids[0],
    ) == ["_drainer-worker:0:2"]


@pytest.mark.parametrize(
    "statuses",
    [{}, {"_drainer-worker:0:1": "UNKNOWN", "_drainer-worker:1:1": "PENDING"}],
)
def test_unknown_worker_inventory_cannot_allocate(worker_database, statuses):
    routine_jobs.reserve_drainer_workers({}, {}, set())
    before = routine_jobs.drainer_worker_intents()
    with pytest.raises(ValueError):
        routine_jobs.reserve_drainer_workers(before, statuses, set())
    assert routine_jobs.drainer_worker_intents() == before


@pytest.mark.parametrize("legacy_count,expected", [(1, 1), (2, 0), (3, 0)])
def test_legacy_workflows_count_toward_same_worker_limit(
    worker_database, legacy_count, expected
):
    assert (
        len(
            routine_jobs.reserve_drainer_workers(
                {}, {}, {f"old-{i}" for i in range(legacy_count)}
            )
        )
        == expected
    )


def test_malformed_internal_worker_identity_fails_closed(worker_database):
    routine_jobs.reserve_drainer_workers({}, {}, set())
    with Session(worker_database) as db:
        db.execute(
            text(
                "UPDATE routine_jobs SET payload=:payload WHERE name='_drainer-worker:0'"
            ),
            {
                "payload": json.dumps(
                    {"generation": 1, "workflow_id": "unrelated-workflow"}
                )
            },
        )
        db.commit()
    with pytest.raises(ValueError, match="invalid drainer worker intent"):
        routine_jobs.drainer_worker_intents()


def test_two_completing_workers_retry_same_snapshot_and_keep_both_successors(
    worker_database, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local
    from types import SimpleNamespace
    from swarm.queues import prepare_drainer_workers

    original_ids = routine_jobs.reserve_drainer_workers({}, {}, set())
    barrier = Barrier(2)
    per_thread = local()
    real_read = routine_jobs.drainer_worker_intents

    def read(*, session=None):
        snapshot = real_read(session=session)
        if session is None and not getattr(per_thread, "read_once", False):
            per_thread.read_once = True
            barrier.wait(timeout=5)
        return snapshot

    class DBOS:
        def list_workflows(self, workflow_ids=None, **_kwargs):
            return [
                SimpleNamespace(workflow_id=wid, status="PENDING")
                for wid in original_ids
                if workflow_ids is None or wid in workflow_ids
            ]

    monkeypatch.setattr(routine_jobs, "drainer_worker_intents", read)
    with ThreadPoolExecutor(2) as workers:
        results = list(
            workers.map(
                lambda wid: prepare_drainer_workers(DBOS(), completing_workflow_id=wid),
                original_ids,
            )
        )
    assert all(results)
    assert set().union(*map(set, results)) == {
        "_drainer-worker:0:2",
        "_drainer-worker:1:2",
    }
    assert [entry["generation"] for entry in real_read().values()] == [2, 2]


@pytest.mark.parametrize(
    "unknown",
    [
        "_drainer-worker:0:01",
        "_drainer-worker:0:99",
        "_drainer-worker:9:1",
        "_drainer-worker:0:1-extra",
    ],
)
def test_lookalike_or_untracked_worker_is_not_retired(worker_database, unknown):
    ids = routine_jobs.reserve_drainer_workers({}, {}, set())
    old = routine_jobs.drainer_worker_intents()
    routine_jobs.reserve_drainer_workers(
        old, {ids[0]: "SUCCESS", ids[1]: "PENDING"}, {ids[1]}
    )
    current = routine_jobs.drainer_worker_intents()
    assert (
        routine_jobs.reserve_drainer_workers(
            current,
            {"_drainer-worker:0:2": "SUCCESS", ids[1]: "PENDING"},
            {ids[1], unknown},
        )
        == []
    )


@pytest.mark.parametrize("mutation", ["finish", "defer", "deregister", "payload"])
def test_stale_claim_owner_cannot_mutate_reassigned_job(freshness_engine, mutation):
    _queue_freshness_job(
        freshness_engine,
        "owned",
        locked_by="luna-drainer:new:0",
        locked_at="2026-01-01",
    )
    operations = {
        "finish": lambda: routine_jobs.complete_job(
            "owned",
            "ok",
            "stale",
            expected_holder="luna-drainer:old:0",
            deregister=True,
        ),
        "defer": lambda: routine_jobs.defer_job(
            "owned", 60, expected_holder="luna-drainer:old:0"
        ),
        "deregister": lambda: routine_jobs.deregister_job(
            "owned", expected_holder="luna-drainer:old:0"
        ),
        "payload": lambda: routine_jobs.update_job_payload(
            "owned", {"stale": True}, expected_holder="luna-drainer:old:0"
        ),
    }
    with Session(freshness_engine) as db:
        before = tuple(
            db.execute(text("SELECT * FROM routine_jobs WHERE name='owned'")).one()
        )
    assert operations[mutation]() is False
    with Session(freshness_engine) as db:
        assert (
            tuple(
                db.execute(text("SELECT * FROM routine_jobs WHERE name='owned'")).one()
            )
            == before
        )
