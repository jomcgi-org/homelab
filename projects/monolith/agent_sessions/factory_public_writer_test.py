"""Real-Postgres tests for the public factory snapshot writer.

factory_public_test.py covers the shaping, which is all pure functions over
dicts. What only a real database can prove is the other half: that the upserts
and the prune survive contact with Postgres types, that an empty factory
publishes a valid empty board instead of raising, and that a task falling off
the rolling window takes its session rows with it while the singleton stays.

The prune is the specific reason this file exists. Its keep-lists go through
expanding bindparams, and an empty list renders as CAST(NULL AS <type>): an
untyped parameter defaults to INTEGER, which Postgres refuses against the TEXT
session_key. Nothing short of a real server catches that, and the first run
after a deploy is exactly the empty-board case.
"""

from __future__ import annotations

import json
import zlib
from datetime import datetime, timedelta, timezone

from sqlmodel import text
from swarm.factory_controls import validate_policy
from swarm.factory_models import FactoryControl, FactoryReceipt
from swarm.models import SwarmNodeRun, SwarmPlanNode, SwarmTask

from agent_sessions.factory_public import write_public_snapshot
from agent_sessions.models import AgentSession, AgentTurn

# Seeded on every identity-bearing column the snapshot must not republish, so
# one substring search over the JSON proves none of them leaked.
SEEDED_EMAIL = "operator@example.test"
ISSUE_NUMBER = 6014
TASK_ID = "task-public-snapshot"
NODE_KEY = "implement_fix"
SESSION_KEY = f"factory:{TASK_ID}:{NODE_KEY}:1"
DIFF_TEXT = "diff --git a/app.py b/app.py\n+one new line\n"

POLICY = {
    "repo": "owner/repo",
    "issue_numbers": [ISSUE_NUMBER],
    "generation": 3,
    "max_tasks": 2,
    "max_turns_per_task": 40,
    "task_budget_usd": 12.0,
    "turn_budget_usd": 2.0,
    "allowed_models": ["opus", "sol"],
    "conductor_model": "opus",
    "worker_model": "sol",
    "base_branch": "main",
    "turn_timeout_seconds": 900,
    "max_attempts": 2,
    "task_timeout_seconds": 14400,
}


def _activity_row(session):
    return session.execute(
        text(
            "SELECT payload, snapshotted_at "
            "FROM public_api.factory_activity_snapshot WHERE id = 1"
        )
    ).first()


def _task_payloads(session) -> dict[int, dict]:
    rows = session.execute(
        text("SELECT issue_number, payload FROM public_api.factory_task_snapshot")
    ).all()
    return {row[0]: row[1] for row in rows}


def _session_rows(session) -> dict[str, tuple[int, dict]]:
    rows = session.execute(
        text(
            "SELECT session_key, issue_number, payload "
            "FROM public_api.factory_session_snapshot"
        )
    ).all()
    return {row[0]: (row[1], row[2]) for row in rows}


def _seed_board(session, *, state: str = "admitted") -> FactoryReceipt:
    """One enabled control, one admitted task, one attempt, one recorded turn."""
    policy = validate_policy(POLICY)
    now = datetime.now(timezone.utc)

    # The intake migration ships the singleton, so configure it rather than
    # inserting a second one.
    control = session.get(FactoryControl, "factory")
    if control is None:
        control = FactoryControl(id="factory", actor=SEEDED_EMAIL)
        session.add(control)
    control.state = "enabled"
    control.actor = SEEDED_EMAIL
    control.policy_json = json.dumps(policy)
    control.admitted_count = 1
    control.version = 1
    session.add(
        SwarmTask(
            id=TASK_ID,
            task_text="publish the factory board",
            repo="owner/repo",
            base_branch="main",
            conductor_model="opus",
            created_at=now - timedelta(hours=1),
        )
    )
    # The receipt and the plan rows carry bare foreign keys with no ORM
    # relationship, so nothing orders the task ahead of them but this flush.
    session.flush()
    receipt = FactoryReceipt(
        repo="owner/repo",
        issue_number=ISSUE_NUMBER,
        generation=3,
        title="Publish the factory board",
        body="the first paragraph\n\nthe second paragraph",
        url=f"https://github.com/owner/repo/issues/{ISSUE_NUMBER}",
        actor=SEEDED_EMAIL,
        state=state,
        task_id=TASK_ID,
        policy_json=json.dumps(policy),
    )
    session.add(receipt)
    session.add(
        SwarmPlanNode(
            task_id=TASK_ID,
            node_key=NODE_KEY,
            kind="work",
            prompt="do the work",
            model="sol",
            deps_json="[]",
            max_cost_usd=4.0,
            side_effects=True,
            created_in_version=0,
        )
    )
    session.flush()

    agent_session = AgentSession(
        local_session_id=SESSION_KEY,
        workspace="/worktrees/factory",
        branch=f"factory/{TASK_ID}",
        repo="owner/repo",
        node_key=NODE_KEY,
        node_attempt=1,
        triggered_by=SEEDED_EMAIL,
        model="sol",
        status="finished",
        ember_session_id="guest-42",
        title="a generated session name",
        voice_summary="a spoken summary",
        created_at=now - timedelta(minutes=50),
        last_turn_at=now - timedelta(minutes=20),
    )
    session.add(agent_session)
    session.flush()

    session.add(
        AgentTurn(
            session_id=agent_session.id,
            seq=1,
            prompt="implement the snapshot writer",
            result_text="done\n\nRATIONALE\n- path: app.py · why: the writer",
            terminal_reason="complete",
            stop_reason="end_turn",
            permission_denials="[]",
            commit_sha="c" * 40,
            base_sha="b" * 40,
            diff_blob=zlib.compress(DIFF_TEXT.encode()),
            diff_truncated=False,
            usage_json=json.dumps(
                {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "activities": [{"type": "bash", "command": "ci lint"}],
                }
            ),
            cost_usd=0.75,
            created_at=now - timedelta(minutes=20),
        )
    )
    session.add(
        SwarmNodeRun(
            task_id=TASK_ID,
            node_key=NODE_KEY,
            attempt=1,
            dispatch_key="dispatch-1",
            session_id=agent_session.id,
            status="succeeded",
            cost_usd=0.75,
            created_at=now - timedelta(minutes=50),
            finished_at=now - timedelta(minutes=20),
        )
    )
    session.flush()
    return receipt


def test_writer_publishes_an_empty_board_for_a_factory_never_configured(session):
    """The first run after a deploy finds nothing, and must still publish.

    The intake migration seeds the control singleton as disabled with an empty
    policy, so this is the state a fresh database is really in. Both prune
    keep-lists are empty here, which is the case that used to fail on the
    untyped bindparam instead of writing a board.
    """
    report = write_public_snapshot(session)

    assert report["tasks"] == 0
    assert report["sessions"] == 0

    row = _activity_row(session)
    assert row is not None, "the singleton must exist even with nothing to show"
    payload = row[0]
    assert payload["active"] == []
    assert payload["queued"] == []
    assert payload["recent"] == []
    assert payload["state"] == "disabled"
    assert payload["snapshotted_at"] == report["snapshotted_at"]
    assert payload["policy"]["max_review_rounds"] == 2

    assert _task_payloads(session) == {}
    assert _session_rows(session) == {}


def test_writer_publishes_the_board_the_task_and_the_session(session):
    _seed_board(session)

    report = write_public_snapshot(session)

    assert report["tasks"] == 1
    assert report["sessions"] == 1

    board = _activity_row(session)[0]
    assert board["state"] == "enabled"
    assert [task["issue_number"] for task in board["active"]] == [ISSUE_NUMBER]
    summary = board["active"][0]
    assert summary["state"] == "in flight"
    assert summary["phase"] == NODE_KEY
    assert summary["nodes"] == [{"node_key": NODE_KEY, "state": "done"}]
    assert summary["allowance_turns"] is not None

    tasks = _task_payloads(session)
    assert list(tasks) == [ISSUE_NUMBER]
    task = tasks[ISSUE_NUMBER]["task"]
    assert task["brief"] == ["the first paragraph", "the second paragraph"]
    assert task["nodes"][0]["attempts"][0]["session_key"] == SESSION_KEY
    digest = task["nodes"][0]["attempts"][0]["turns"][0]
    assert digest["seq"] == 1
    assert digest["activities"] == [{"type": "bash", "command": "ci lint"}]
    # A walkthrough digest never carries the diff; that is the session page.
    assert "diff" not in digest

    sessions = _session_rows(session)
    assert list(sessions) == [SESSION_KEY]
    issue_number, payload = sessions[SESSION_KEY]
    assert issue_number == ISSUE_NUMBER
    assert payload["session"]["key"] == SESSION_KEY
    assert payload["session"]["node_key"] == NODE_KEY
    assert payload["session"]["attempt"] == 1
    assert payload["session"]["turn_count"] == 1
    assert payload["session"]["guest_bound"] is True
    turn = payload["turns"][0]
    assert turn["diff"] == DIFF_TEXT
    assert turn["diff_truncated"] is False
    assert turn["activities"] == [{"type": "bash", "command": "ci lint"}]
    assert turn["rationale"]["parse_status"] == "parsed"
    assert turn["usage"] == {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_read_tokens": None,
    }


def test_no_published_payload_carries_an_actor_or_an_email(session):
    _seed_board(session)
    write_public_snapshot(session)

    published = [_activity_row(session)[0]]
    published.extend(_task_payloads(session).values())
    published.extend(payload for _, payload in _session_rows(session).values())
    assert len(published) == 3

    for payload in published:
        encoded = json.dumps(payload)
        # Match the JSON key, not the bare word: "factory" contains "actor".
        assert '"actor":' not in encoded
        assert '"triggered_by":' not in encoded
        assert SEEDED_EMAIL not in encoded
        # The session's generated name and voice summary are private too.
        assert "a generated session name" not in encoded
        assert "a spoken summary" not in encoded


def test_a_task_that_falls_off_the_board_is_pruned_with_its_sessions(session):
    receipt = _seed_board(session)
    write_public_snapshot(session)
    assert list(_task_payloads(session)) == [ISSUE_NUMBER]
    assert list(_session_rows(session)) == [SESSION_KEY]

    session.delete(receipt)
    session.flush()

    report = write_public_snapshot(session)

    assert report["tasks"] == 0
    assert report["sessions"] == 0
    assert _task_payloads(session) == {}
    assert _session_rows(session) == {}
    # The board itself is a singleton: it is rewritten, never pruned away.
    board = _activity_row(session)[0]
    assert board is not None
    assert board["active"] == []
    assert board["snapshotted_at"] == report["snapshotted_at"]


def test_a_second_run_replaces_the_payload_in_place(session):
    _seed_board(session)
    first = write_public_snapshot(session)
    first_board = _activity_row(session)

    second = write_public_snapshot(session)
    second_board = _activity_row(session)

    assert second["snapshotted_at"] != first["snapshotted_at"]
    assert second_board[0]["snapshotted_at"] == second["snapshotted_at"]
    assert second_board[1] > first_board[1]
    # Upsert, not insert: one board row, one task row, one session row.
    assert (
        session.execute(
            text("SELECT count(*) FROM public_api.factory_activity_snapshot")
        ).scalar_one()
        == 1
    )
    assert list(_task_payloads(session)) == [ISSUE_NUMBER]
    assert list(_session_rows(session)) == [SESSION_KEY]


def test_public_reader_can_read_back_what_the_writer_published(session, pg):
    """The writer's rows are reachable through the role the public tier uses."""
    from sqlmodel import Session, create_engine

    _seed_board(session)
    write_public_snapshot(session)
    # The savepoint fixture holds the writer's rows inside an uncommitted outer
    # transaction, so a second connection cannot see them. Check the grant on
    # the tables themselves, which is what a public route depends on.
    engine = create_engine(pg.url)
    try:
        with Session(engine) as reader:
            reader.execute(text("SET ROLE public_reader"))
            for table in (
                "factory_activity_snapshot",
                "factory_task_snapshot",
                "factory_session_snapshot",
            ):
                reader.execute(text(f"SELECT * FROM public_api.{table}")).all()
    finally:
        engine.dispose()
