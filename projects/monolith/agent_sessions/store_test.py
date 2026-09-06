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
            if claimed_by or ember_id:
                assert claim["outcome_unknown"] is True
                assert store.get_session(session, row.id).status == "failed"
                assert store.get_session(session, row.id).ember_session_id == ember_id
                assert store.get_turn(session, row.id, 1).prompt == "original prompt"
                assert store.get_pending_message(session, row.id, 2) is not None
                return
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


def test_hung_recovery_stolen_claim_makes_refresh_return_false(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    try:
        with Session(engine) as session:
            row = store.create_session(
                session, "hung-stolen", "<guest>", "main", repo="jomcgi/homelab"
            )
            row.ember_session_id = "ember-hung"
            row.ember_session_token = "token"
            session.add_all(
                [
                    row,
                    PendingMessage(
                        session_id=row.id,
                        seq=1,
                        message_text="hung",
                        claimed_by_replica="hung-pod:old",
                        claimed_at=now,
                        dispatch_count=1,
                        last_dispatch_at=now - timedelta(seconds=601),
                    ),
                ]
            )
            session.commit()

            claim = store.claim_hung_zombie_session_recovery(
                session,
                row.id,
                now - timedelta(seconds=600),
                now,
                "ember-hung",
            )

            assert claim is not None
            assert claim["outcome_unknown"] is True
            assert store.finalize_zombie_session_recovery(session, claim, now) is None
            assert (
                store.claim_pending_message_for_session_sync(row.id, "hung-pod:new")
                is None
            )
            assert store.refresh_claim_sync(row.id, 1, "hung-pod:old") is False
            session.expire_all()
            assert store.get_pending_message(session, row.id, 1) is None
            assert (
                store.get_turn(session, row.id, 1).stop_reason
                == store.UNKNOWN_INVOCATION
            )
    finally:
        _restore_schemas(schemas)


def test_claim_records_dispatch_count_and_time(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            row = store.create_session(session, "dispatch-trace", "<guest>", "main")
            pending = store.create_pending_message(session, row.id, "run me")
            session_id = row.id
            turn_seq = pending.seq

        assert store.claim_pending_message_for_session_sync(session_id, "pod-a") == 1

        with Session(engine) as session:
            first = store.get_pending_message(session, session_id, turn_seq)
            assert first.dispatch_count == 1
            assert isinstance(first.last_dispatch_at, datetime)
        store.mark_turn_interrupted_sync(session_id, turn_seq, "pod-a")
        store.release_pending_message_claim_sync(session_id, turn_seq, "pod-a")
        assert store.claim_pending_message_for_session_sync(session_id, "pod-b") == 1

        with Session(engine) as session:
            second = store.get_pending_message(session, session_id, turn_seq)
            assert second.dispatch_count == 2
            assert isinstance(second.last_dispatch_at, datetime)
    finally:
        _restore_schemas(schemas)


def test_release_preserves_interrupted_turn_and_pending_row(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            row = store.create_session(session, "interrupted", "<guest>", "main")
            pending = store.create_pending_message(session, row.id, "retry me", "luna")
            session_id = row.id
            turn_seq = pending.seq

        assert store.claim_pending_message_for_session_sync(session_id, "pod-a") == 1
        store.mark_turn_interrupted_sync(session_id, turn_seq)
        store.release_pending_message_claim_sync(session_id, turn_seq, "pod-a")

        with Session(engine) as session:
            interrupted = store.get_turn(session, session_id, turn_seq)
            retry = store.get_pending_message(session, session_id, turn_seq)
            assert interrupted.terminal_reason == "interrupted"
            assert interrupted.stop_reason == "brick_preempted"
            assert retry.claimed_by_replica is None
            assert store.get_session(session, session_id).status == "recovering"
    finally:
        _restore_schemas(schemas)


def test_preemption_replacement_demotes_old_binding_and_marks_workspace_loss(
    monkeypatch, tmp_path
):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            row = store.create_session(session, "preempted", "<guest>", "main")
            store.set_ember_session(
                session,
                row.id,
                "ember-old",
                "token-old",
                None,
                "lineage-old",
                cli_session_id="cli-old",
            )
            replaced = store.replace_ember_session_after_preemption(
                session,
                row.id,
                "ember-new",
                "token-new",
                None,
                "lineage-new",
            )

            assert replaced.ember_session_id == "ember-new"
            assert replaced.ember_lineage_id == "lineage-new"
            assert replaced.cli_session_id is None
            assert replaced.prior_ember_lineage_id == "lineage-old"
            assert replaced.prior_cli_session_id == "cli-old"
            assert replaced.recovery_workspace_loss is True
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


def test_write_progress_sync_does_not_contaminate_unclaimed_rows(monkeypatch, tmp_path):
    """Late progress cannot mark an untouched lane head as attempted."""
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
        assert result == "no_row"
        with Session(engine) as session:
            rows = session.exec(
                select(PendingMessage).order_by(PendingMessage.seq)
            ).all()
            assert rows[0].partial_text is None
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


@pytest.fixture
def uncertain_lane(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            row = store.create_session(session, "uncertain", "<guest>", "main")
            row.ember_session_id = "guest-investigate"
            row.ember_lineage_id = "lineage-retain"
            row.progress_token = "old-progress"
            session.add(row)
            session.commit()
            session_id = row.id
            store.create_pending_message(
                session, session_id, "original prompt", "terra"
            )
            store.create_pending_message(
                session, session_id, "queued successor", "terra"
            )
        assert store.claim_pending_message_for_session_sync(session_id, "owner-1") == 1
        assert (
            store.write_progress_sync(
                "old-progress", "partial review", [{"tool": "read", "path": "a.py"}]
            )
            == "ok"
        )
        yield engine, session_id
    finally:
        _restore_schemas(schemas)


def test_unknown_release_preserves_evidence_and_holds_session(uncertain_lane):
    engine, session_id = uncertain_lane
    store.release_pending_message_claim_sync(session_id, 1, "owner-1")
    with Session(engine) as session:
        turn = store.get_turn(session, session_id, 1)
        usage = json.loads(turn.usage_json)
        assert turn.prompt == "original prompt"
        assert turn.result_text == "partial review"
        assert turn.stop_reason == store.UNKNOWN_INVOCATION
        assert turn.terminal_reason == "error"
        assert turn.cost_usd is None
        assert usage["activities"] == [{"tool": "read", "path": "a.py"}]
        assert usage["recovery"]["dispatch_count"] == 1
        assert usage["recovery"]["claim_owner"] == "owner-1"
        assert usage["recovery"]["last_dispatch_at"] is not None
        row = store.get_session(session, session_id)
        assert row.ember_session_id == "guest-investigate"
        assert row.ember_lineage_id == "lineage-retain"
        assert row.progress_token is None
        assert store.get_pending_message(session, session_id, 1) is None
        assert store.get_pending_message(session, session_id, 2) is not None
        with pytest.raises(store.SessionOutcomeUnknown, match="start a new session"):
            store.create_pending_message(session, session_id, "ordinary send")
        session.rollback()
        store.update_session_status(session, session_id, "running")
        assert store.get_session(session, session_id).status == "failed"
        assert store.activate_session_after_enqueue(session, session_id) is False
    assert store.claim_pending_message_for_session_sync(session_id, "late-task") is None
    assert store.get_all_pending_messages_sync() == []
    assert (
        store.write_progress_sync("old-progress", "late guest output")
        == "unknown_token"
    )
    with Session(engine) as session:
        assert store.get_pending_message(session, session_id, 2).partial_text is None


def test_unknown_hold_blocks_claim_even_if_status_was_reopened(uncertain_lane):
    engine, session_id = uncertain_lane
    store.release_pending_message_claim_sync(session_id, 1, "owner-1")
    with Session(engine) as session:
        row = store.get_session(session, session_id)
        row.status = "running"
        session.add(row)
        session.commit()
    assert store.claim_pending_message_for_session_sync(session_id, "another") is None
    assert store.get_all_pending_messages_sync() == []


def test_direct_claim_stops_a_previously_released_attempt(uncertain_lane):
    engine, session_id = uncertain_lane
    with Session(engine) as session:
        pending = store.get_pending_message(session, session_id, 1)
        pending.claimed_by_replica = None
        pending.claimed_at = None
        session.add(pending)
        session.commit()
    assert (
        store.claim_pending_message_for_session_sync(session_id, "replacement") is None
    )
    with Session(engine) as session:
        assert (
            store.get_turn(session, session_id, 1).stop_reason
            == store.UNKNOWN_INVOCATION
        )


def test_unknown_reconciliation_is_idempotent_and_owner_fenced(uncertain_lane):
    engine, session_id = uncertain_lane
    assert store.finish_unknown_pending_sync(session_id, 1, "other", 1, "lost") is False
    assert (
        store.finish_unknown_pending_sync(session_id, 1, "owner-1", 2, "lost") is False
    )
    from threading import Barrier

    barrier = Barrier(2)

    def reconcile():
        barrier.wait(timeout=5)
        return store.finish_unknown_pending_sync(session_id, 1, "owner-1", 1, "lost")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reconcile(), range(2)))
    assert sorted(results) == [False, True]
    assert store.reclaim_stale_claims_sync() == 0
    store.release_pending_message_claim_sync(session_id, 1, "owner-1")
    with Session(engine) as session:
        assert len(store.get_turns(session, session_id)) == 1


def test_stale_reclaim_rechecks_heartbeat_after_candidate_read(
    uncertain_lane, monkeypatch
):
    from threading import Event, current_thread, main_thread

    engine, session_id = uncertain_lane
    with Session(engine) as session:
        pending = store.get_pending_message(session, session_id, 1)
        pending.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        session.add(pending)
        session.commit()
    candidates_read, continue_reclaim = Event(), Event()
    original = store._lock_session

    def pause_reclaimer(session, session_id):
        if current_thread() is not main_thread():
            candidates_read.set()
            assert continue_reclaim.wait(5)
        return original(session, session_id)

    monkeypatch.setattr(store, "_lock_session", pause_reclaimer)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reclaim = pool.submit(store.reclaim_stale_claims_sync)
        assert candidates_read.wait(5)
        assert store.refresh_claim_sync(session_id, 1, "owner-1") is True
        continue_reclaim.set()
        assert reclaim.result(timeout=5) == 0
    with Session(engine) as session:
        assert store.get_turn(session, session_id, 1) is None
        assert (
            store.get_pending_message(session, session_id, 1).claimed_by_replica
            == "owner-1"
        )


def test_expired_attempt_is_recorded_once(uncertain_lane):
    engine, session_id = uncertain_lane
    with Session(engine) as session:
        pending = store.get_pending_message(session, session_id, 1)
        pending.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        session.add(pending)
        session.commit()
    assert store.reclaim_stale_claims_sync() == 1
    assert store.reclaim_stale_claims_sync() == 0
    with Session(engine) as session:
        turn = store.get_turn(session, session_id, 1)
        assert json.loads(turn.usage_json)["recovery"]["cause"] == "lease_expired"
        assert turn.result_text == "partial review"


def test_preemption_permission_cannot_authorize_a_later_unknown_attempt(uncertain_lane):
    engine, session_id = uncertain_lane
    store.mark_turn_interrupted_sync(session_id, 1, "owner-1")
    store.release_pending_message_claim_sync(session_id, 1, "owner-1")
    assert store.claim_pending_message_for_session_sync(session_id, "owner-2") == 1
    # The old owner cannot consume this retry or turn it into a failure.
    store.release_pending_message_claim_sync(session_id, 1, "owner-1")
    with Session(engine) as session:
        pending = store.get_pending_message(session, session_id, 1)
        assert pending.dispatch_count == 2
        assert pending.claimed_by_replica == "owner-2"
    store.release_pending_message_claim_sync(session_id, 1, "owner-2")
    assert store.claim_pending_message_for_session_sync(session_id, "owner-3") is None
    with Session(engine) as session:
        turn = store.get_turn(session, session_id, 1)
        assert turn.stop_reason == store.UNKNOWN_INVOCATION
        assert json.loads(turn.usage_json)["recovery"]["dispatch_count"] == 2


def test_legacy_bound_zombie_is_held_without_dispatch_trace(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    try:
        with Session(engine) as session:
            row = store.create_session(session, "legacy-bound", "<guest>", "main")
            row.ember_session_id = "guest-unknown"
            session.add(row)
            session.add(
                PendingMessage(
                    session_id=row.id,
                    seq=1,
                    message_text="legacy",
                    created_at=now - timedelta(seconds=181),
                )
            )
            session.commit()
            claim = store.claim_zombie_session_recovery(
                session, row.id, now - timedelta(seconds=180), now
            )
            assert claim["outcome_unknown"] is True
            assert (
                store.get_session(session, row.id).ember_session_id == "guest-unknown"
            )
            assert (
                store.get_turn(session, row.id, 1).stop_reason
                == store.UNKNOWN_INVOCATION
            )
    finally:
        _restore_schemas(schemas)


def _successful_uncertain_turn():
    from agent_sessions.transport import Turn

    return Turn(
        result="committed result",
        terminal_reason="completed",
        stop_reason="end_turn",
        is_error=False,
        permission_denials=[],
        num_turns=1,
        session_id="cli-done",
        usage={},
        total_cost_usd=0.01,
        duration_ms=1,
        activities=[],
    )


def test_unknown_record_and_queue_disposition_roll_back_together(
    uncertain_lane, monkeypatch
):
    from sqlalchemy import event

    engine, session_id = uncertain_lane

    def reject_pending_delete(connection, cursor, statement, parameters, context, many):
        if statement.startswith("DELETE FROM") and "pending_messages" in statement:
            raise RuntimeError("injected queue disposition failure")

    event.listen(engine, "before_cursor_execute", reject_pending_delete)
    try:
        with pytest.raises(RuntimeError, match="queue disposition"):
            store.release_pending_message_claim_sync(session_id, 1, "owner-1")
    finally:
        event.remove(engine, "before_cursor_execute", reject_pending_delete)
    with Session(engine) as session:
        assert store.get_turn(session, session_id, 1) is None
        assert store.get_session(session, session_id).status == "running"
        assert store.get_session(session, session_id).progress_token == "old-progress"
        assert (
            store.get_pending_message(session, session_id, 1).partial_text
            == "partial review"
        )
    assert store.release_pending_message_claim_sync(session_id, 1, "owner-1") is True


def test_late_completion_cannot_overwrite_unknown_evidence(uncertain_lane):
    engine, session_id = uncertain_lane
    store.release_pending_message_claim_sync(session_id, 1, "owner-1")
    with pytest.raises(store.SessionOutcomeUnknown):
        store.persist_turn_from_pending_sync(
            session_id,
            1,
            "original prompt",
            _successful_uncertain_turn(),
            "done",
            "completed",
            "cli-done",
            "terra",
            "owner-1",
            1,
        )
    with Session(engine) as session:
        assert store.get_turn(session, session_id, 1).result_text == "partial review"
        assert store.get_session(session, session_id).cli_session_id is None


def test_completion_and_unknown_reconciliation_have_one_durable_winner(uncertain_lane):
    from threading import Barrier

    engine, session_id = uncertain_lane
    barrier = Barrier(2)

    def complete():
        barrier.wait(timeout=5)
        try:
            store.persist_turn_from_pending_sync(
                session_id,
                1,
                "original prompt",
                _successful_uncertain_turn(),
                "done",
                "completed",
                "cli-done",
                "terra",
                "owner-1",
                1,
            )
            return "completed"
        except store.SessionOutcomeUnknown:
            return "held"

    def hold():
        barrier.wait(timeout=5)
        return store.finish_unknown_pending_sync(session_id, 1, "owner-1", 1, "lost")

    with ThreadPoolExecutor(max_workers=2) as pool:
        completion = pool.submit(complete)
        unknown = pool.submit(hold)
        winner, held = completion.result(timeout=5), unknown.result(timeout=5)
    assert (winner, held) in {("completed", False), ("held", True)}
    with Session(engine) as session:
        turns = store.get_turns(session, session_id)
        assert len(turns) == 1
        assert turns[0].result_text == (
            "partial review" if held else "committed result"
        )
        assert store.get_pending_message(session, session_id, 1) is None
        assert store.get_session(session, session_id).status == (
            "failed" if held else "completed"
        )


def test_completion_requires_the_exact_dispatch_attempt(uncertain_lane):
    engine, session_id = uncertain_lane
    with pytest.raises(store.PendingClaimLost):
        store.persist_turn_from_pending_sync(
            session_id,
            1,
            "original prompt",
            _successful_uncertain_turn(),
            "done",
            "completed",
            "cli-done",
            "terra",
            "owner-1",
            2,
        )
    with Session(engine) as session:
        assert store.get_turn(session, session_id, 1) is None
        assert store.get_pending_message(session, session_id, 1) is not None


def test_late_progress_after_completion_does_not_hold_untouched_successor(
    uncertain_lane,
):
    engine, session_id = uncertain_lane
    store.persist_turn_from_pending_sync(
        session_id,
        1,
        "original prompt",
        _successful_uncertain_turn(),
        "done",
        "completed",
        "cli-done",
        "terra",
        "owner-1",
        1,
    )
    assert (
        store.write_progress_sync(
            "old-progress", "late previous output", [{"tool": "read", "path": "old.py"}]
        )
        == "no_row"
    )
    with Session(engine) as session:
        successor = store.get_pending_message(session, session_id, 2)
        assert successor.dispatch_count == 0
        assert successor.partial_text is None
        assert successor.partial_activities is None
    assert store.claim_pending_message_for_session_sync(session_id, "owner-2") == 2
    with Session(engine) as session:
        assert store.get_turn(session, session_id, 2) is None
        assert store.get_pending_message(session, session_id, 2).dispatch_count == 1
