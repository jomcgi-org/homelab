import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions import store
from agent_sessions.constants import (
    LEGACY_QWEN_SYNTHETIC_PROMPT,
    SYNTHETIC_SESSION_PREFIX,
)
from agent_sessions.models import AgentSession, AgentTurn, PendingMessage


def test_lexical_search_excludes_synthetic_sessions():
    captured = {}

    class FakeSession:
        def exec(self, statement, params):
            captured["sql"] = str(statement)
            captured["params"] = params
            return []

    assert store.lexical_search(FakeSession(), "qwen") == []
    assert "s.local_session_id NOT LIKE :synthetic_prefix" in captured["sql"]
    assert "first_turn.session_id = s.id" in captured["sql"]
    assert captured["params"]["synthetic_prefix"] == (f"{SYNTHETIC_SESSION_PREFIX}%")
    assert captured["params"]["qwen_synthetic_prompt"] == LEGACY_QWEN_SYNTHETIC_PROMPT


def _database(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'store_test.db'}",
        connect_args={"check_same_thread": False},
    )
    schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(store, "get_engine", lambda: engine)
    return engine, schemas


def _restore_schemas(schemas):
    for table in SQLModel.metadata.tables.values():
        if table.name in schemas:
            table.schema = schemas[table.name]


def test_write_progress_sync_updates_claimed_row(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            agent = AgentSession(
                local_session_id="local",
                workspace="workspace",
                branch="main",
                progress_token="token",
            )
            session.add(agent)
            session.commit()
            session.refresh(agent)
            session.add_all(
                [
                    PendingMessage(
                        session_id=agent.id,
                        seq=1,
                        message_text="first",
                        claimed_by_replica=None,
                    ),
                    PendingMessage(
                        session_id=agent.id,
                        seq=2,
                        message_text="second",
                        claimed_by_replica="replica-a",
                    ),
                ]
            )
            session.commit()

        assert store.write_progress_sync("token", "working") == "ok"
        with Session(engine) as session:
            rows = session.exec(
                select(PendingMessage).order_by(PendingMessage.seq)
            ).all()
            assert rows[0].partial_text is None
            assert rows[1].partial_text == "working"
    finally:
        _restore_schemas(schemas)


def test_create_session_persists_optional_system_prompt(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            prompted = store.create_session(
                session,
                "prompted",
                "<guest>",
                "main",
                system_prompt="X",
            )
            unprompted = store.create_session(session, "unprompted", "<guest>", "main")

            assert prompted.system_prompt == "X"
            assert unprompted.system_prompt is None
    finally:
        _restore_schemas(schemas)


def test_create_session_persists_normalized_triggered_by(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            triggered = store.create_session(
                session,
                "triggered",
                "<guest>",
                "main",
                triggered_by="  EXAMPLE@EXAMPLE.COM  ",
            )
            untriggered = store.create_session(
                session, "untriggered", "<guest>", "main"
            )
            blank = store.create_session(
                session, "blank", "<guest>", "main", triggered_by="   "
            )

            assert triggered.triggered_by == "example@example.com"
            assert untriggered.triggered_by is None
            # Whitespace-only must land as NULL, not "". An empty string passes a
            # NULL check but matches no caller, so it would own rows nobody reads.
            assert blank.triggered_by is None
    finally:
        _restore_schemas(schemas)


def test_create_session_persists_and_queries_workflow_id(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            linked = store.create_session(
                session,
                "linked",
                "<guest>",
                "main",
                workflow_id="wf-123",
            )
            unlinked = store.create_session(session, "unlinked", "<guest>", "main")

            assert linked.workflow_id == "wf-123"
            assert unlinked.workflow_id is None
            queried = session.exec(
                select(AgentSession).where(AgentSession.workflow_id == "wf-123")
            ).one()
            assert queried.id == linked.id
            indexes = inspect(engine).get_indexes("agent_sessions")
            assert any("workflow_id" in index["column_names"] for index in indexes)
    finally:
        _restore_schemas(schemas)


def test_sessions_for_workflow_returns_only_matching_rows(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            matching = store.create_session(
                session, "matching", "<guest>", "main", workflow_id="wf-1"
            )
            store.create_session(
                session, "other", "<guest>", "main", workflow_id="wf-2"
            )
            assert store.sessions_for_workflow(session, "wf-1") == [matching]
    finally:
        _restore_schemas(schemas)


def test_find_zombie_sessions_requires_old_zero_turn_session(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    try:
        with Session(engine) as session:
            zombie = store.create_session(session, "zombie", "<guest>", "main")
            fresh = store.create_session(session, "fresh", "<guest>", "main")
            finished = store.create_session(session, "finished", "<guest>", "main")
            store.create_session(session, "no-message", "<guest>", "main")
            active = store.create_session(session, "active", "<guest>", "main")
            old = now - timedelta(seconds=181)
            zombie_pending = PendingMessage(
                session_id=zombie.id,
                seq=1,
                message_text="retry me",
                created_at=old,
            )
            finished_pending = PendingMessage(
                session_id=finished.id,
                seq=2,
                message_text="already finished",
                created_at=old,
            )
            active_head = PendingMessage(
                session_id=active.id,
                seq=1,
                message_text="still executing",
                claimed_by_replica="live-pod",
                claimed_at=now,
                created_at=old,
            )
            session.add_all(
                [
                    zombie_pending,
                    PendingMessage(session_id=fresh.id, seq=1, message_text="not old"),
                    AgentTurn(
                        session_id=finished.id,
                        seq=1,
                        prompt="already ran",
                        result_text="done",
                    ),
                    finished_pending,
                    active_head,
                    PendingMessage(
                        session_id=active.id,
                        seq=2,
                        message_text="queued follow-up",
                        created_at=old,
                    ),
                ]
            )
            session.commit()

            candidates = store.find_zombie_session_ids(
                session, now - timedelta(seconds=180), now
            )

            assert candidates == [zombie.id]
    finally:
        _restore_schemas(schemas)


def test_zombie_recovery_cas_has_one_winner(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    try:
        with Session(engine) as session:
            row = store.create_session(session, "zombie-cas", "<guest>", "main")
            session.add_all(
                [
                    PendingMessage(
                        session_id=row.id,
                        seq=1,
                        message_text="original",
                        created_at=now - timedelta(seconds=181),
                    ),
                ]
            )
            session.commit()
            session_id = row.id

        def claim():
            with Session(engine) as session:
                return store.claim_zombie_session_recovery(
                    session,
                    session_id,
                    now - timedelta(seconds=180),
                    now,
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _: claim(), range(2)))

        assert sum(claimed is not None for claimed in claims) == 1
        with Session(engine) as session:
            assert store.get_session(session, session_id).status == "recovering"
    finally:
        _restore_schemas(schemas)


@pytest.mark.parametrize(
    ("repo", "claimed_by", "ember_id", "expected_workspace_loss"),
    [
        (None, None, None, False),
        ("jomcgi/homelab", "dead-pod", "ember-dead", True),
    ],
)
def test_finalize_zombie_recovery_reuses_lane_head_before_follow_up(
    monkeypatch,
    tmp_path,
    repo,
    claimed_by,
    ember_id,
    expected_workspace_loss,
):
    engine, schemas = _database(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    try:
        with Session(engine) as session:
            row = store.create_session(
                session, "zombie", "<guest>", "main", model="luna", repo=repo
            )
            row.ember_session_id = ember_id
            row.ember_session_token = "token" if ember_id else None
            session.add_all(
                [
                    row,
                    PendingMessage(
                        session_id=row.id,
                        seq=1,
                        message_text="original prompt",
                        model="luna",
                        claimed_by_replica=claimed_by,
                        claimed_at=now - timedelta(seconds=181) if claimed_by else None,
                        created_at=now - timedelta(seconds=181),
                    ),
                    PendingMessage(
                        session_id=row.id,
                        seq=2,
                        message_text="queued follow-up",
                        model="luna",
                    ),
                ]
            )
            session.commit()

            claim = store.claim_zombie_session_recovery(
                session,
                row.id,
                now - timedelta(seconds=180),
                now,
            )
            assert claim is not None
            assert claim["recovery_workspace_loss"] is expected_workspace_loss
            assert (
                store.get_session(session, row.id).recovery_workspace_loss
                is expected_workspace_loss
            )
            if ember_id:
                store.clear_ember_bindings_by_ember_id(session, ember_id)
            retry_seq = store.finalize_zombie_session_recovery(session, claim, now)

            assert retry_seq == 1
            assert store.get_turn(session, row.id, 1) is None
            pending = session.exec(
                select(PendingMessage)
                .where(PendingMessage.session_id == row.id)
                .order_by(PendingMessage.seq)
            ).all()
            assert [(message.seq, message.message_text) for message in pending] == [
                (1, "original prompt"),
                (2, "queued follow-up"),
            ]
            assert pending[0].claimed_by_replica is None
            assert pending[0].claimed_at is None
            recovered_session = store.get_session(session, row.id)
            assert recovered_session.status == "running"
            assert recovered_session.recovery_workspace_loss is None
            assert recovered_session.recovery_completed_at is not None
            assert (
                store.find_zombie_session_ids(
                    session, now - timedelta(seconds=180), now
                )
                == []
            )
    finally:
        _restore_schemas(schemas)


def test_zombie_recovery_cas_abandonment_restores_running(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    try:
        with Session(engine) as session:
            row = store.create_session(session, "vanished-head", "<guest>", "main")
            session.add(
                PendingMessage(
                    session_id=row.id,
                    seq=1,
                    message_text="vanishes after cas",
                    created_at=now - timedelta(seconds=181),
                )
            )
            session.commit()
            session_id = row.id

            class NoPending:
                def first(self):
                    return None

            monkeypatch.setattr(session, "exec", lambda _statement: NoPending())
            claim = store.claim_zombie_session_recovery(
                session,
                session_id,
                now - timedelta(seconds=180),
                now,
            )

            assert claim is None

        with Session(engine) as verify:
            recovered = store.get_session(verify, session_id)
            assert recovered.status == "running"
            assert recovered.recovery_workspace_loss is None
    finally:
        _restore_schemas(schemas)


def test_live_claim_invariant_is_shared_by_detector_and_reclaimer(
    monkeypatch, tmp_path
):
    assert store.RECLAIM_LEASE == timedelta(seconds=30)
    engine, schemas = _database(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    try:
        with Session(engine) as session:
            row = store.create_session(session, "shared-lease", "<guest>", "main")
            session.add_all(
                [
                    PendingMessage(
                        session_id=row.id,
                        seq=1,
                        message_text="stale head",
                        claimed_by_replica="dead-pod",
                        claimed_at=now - timedelta(seconds=31),
                        created_at=now - timedelta(seconds=181),
                    ),
                    PendingMessage(
                        session_id=row.id,
                        seq=2,
                        message_text="fresh claimed follow-up",
                        claimed_by_replica="live-pod",
                        claimed_at=now - timedelta(seconds=29),
                    ),
                ]
            )
            session.commit()

            assert (
                store.find_zombie_session_ids(
                    session, now - timedelta(seconds=180), now
                )
                == []
            )
            assert store.reclaim_stale_claims_sync() == 0
            session.expire_all()
            assert (
                store.get_pending_message(session, row.id, 1).claimed_by_replica
                == "dead-pod"
            )
    finally:
        _restore_schemas(schemas)


def test_zombie_detector_limits_each_sweep_to_five(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    try:
        with Session(engine) as session:
            rows = [
                AgentSession(
                    local_session_id=f"bounded-{index}",
                    workspace="<guest>",
                    branch="main",
                )
                for index in range(6)
            ]
            session.add_all(rows)
            session.flush()
            session.add_all(
                [
                    PendingMessage(
                        session_id=row.id,
                        seq=1,
                        message_text="old lane head",
                        created_at=now - timedelta(seconds=181),
                    )
                    for row in rows
                ]
            )
            session.commit()

            candidates = store.find_zombie_session_ids(
                session, now - timedelta(seconds=180), now
            )

            assert len(candidates) == 5
    finally:
        _restore_schemas(schemas)


def test_write_progress_sync_stores_activities(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    activities = [{"type": "tool", "name": "shell"}]
    try:
        with Session(engine) as session:
            agent = AgentSession(
                local_session_id="local",
                workspace="workspace",
                branch="main",
                progress_token="token",
            )
            session.add(agent)
            session.commit()
            session.refresh(agent)
            session.add(
                PendingMessage(
                    session_id=agent.id,
                    seq=1,
                    message_text="first",
                    claimed_by_replica="replica-a",
                )
            )
            session.commit()

        assert store.write_progress_sync("token", "working", activities) == "ok"
        with Session(engine) as session:
            row = session.exec(select(PendingMessage)).one()
            assert json.loads(row.partial_activities) == activities
    finally:
        _restore_schemas(schemas)


def test_write_progress_sync_unknown_token_returns_unknown_token(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        assert store.write_progress_sync("missing", "working") == "unknown_token"
    finally:
        _restore_schemas(schemas)


def test_write_progress_sync_without_pending_row_returns_no_row(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            session.add(
                AgentSession(
                    local_session_id="local",
                    workspace="workspace",
                    branch="main",
                    progress_token="token",
                )
            )
            session.commit()
        assert store.write_progress_sync("token", "working") == "no_row"
    finally:
        _restore_schemas(schemas)


def test_write_progress_sync_falls_back_to_unclaimed_row(monkeypatch, tmp_path):
    """Fallback updates lowest seq unclaimed row when no claimed row exists."""
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            agent = AgentSession(
                local_session_id="local",
                workspace="workspace",
                branch="main",
                progress_token="token",
            )
            session.add(agent)
            session.commit()
            session.refresh(agent)
            session.add_all(
                [
                    PendingMessage(
                        session_id=agent.id,
                        seq=1,
                        message_text="first",
                        claimed_by_replica=None,
                    ),
                    PendingMessage(
                        session_id=agent.id,
                        seq=2,
                        message_text="second",
                        claimed_by_replica=None,
                    ),
                ]
            )
            session.commit()

        result = store.write_progress_sync("token", "working")
        assert result == "ok"
        with Session(engine) as session:
            rows = session.exec(
                select(PendingMessage).order_by(PendingMessage.seq)
            ).all()
            assert rows[0].partial_text == "working"
            assert rows[1].partial_text is None
    finally:
        _restore_schemas(schemas)


def test_discord_thread_binds_at_most_one_session(monkeypatch, tmp_path):
    """A thread can never fan out to two sessions.

    The unique constraint is what makes session_id_for_thread a lookup rather
    than a choice: without it a second /agent in the same thread would create a
    rival session and turns would land in whichever one the query happened to
    return first.
    """
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            store.create_session(
                session, "local-1", "<guest>", "main", "luna", discord_thread="t-1"
            )
            with pytest.raises(IntegrityError):
                store.create_session(
                    session, "local-2", "<guest>", "main", "luna", discord_thread="t-1"
                )
            session.rollback()

        # Unbound sessions are unaffected: many NULLs are allowed under the
        # constraint, which is what keeps the UI and MCP lanes working.
        with Session(engine) as session:
            store.create_session(session, "local-3", "<guest>", "main", "luna")
            store.create_session(session, "local-4", "<guest>", "main", "luna")
            rows = session.exec(
                select(AgentSession).where(AgentSession.discord_thread.is_(None))
            ).all()
            assert len(rows) == 2
    finally:
        _restore_schemas(schemas)
