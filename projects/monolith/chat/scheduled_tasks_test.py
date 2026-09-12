"""Hermetic coverage for durable scheduled task claims and advancement."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine

from chat import outbox as discord_outbox
from chat.models import DiscordOutbox, ScheduledTask, ScheduledTaskOccurrence
from chat.scheduled_tasks import (
    MAX_GENERATION_ATTEMPTS,
    cancel_task,
    claim_due,
    create_task,
    drain_once,
    finalize_claim,
    list_tasks,
    next_cron_at,
    release_claim,
)


@pytest.fixture(name="engine")
def engine_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'scheduled.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    original = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original:
            table.schema = original[table.name]


def _insert_due(engine, *, kind="reminder", cron=None) -> tuple[int, datetime]:
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        if kind == "reminder":
            task = create_task(
                session,
                channel_id="123",
                author_id="456",
                task_kind="reminder",
                schedule_kind="one_shot",
                due_at=now + timedelta(minutes=1),
                content="stand up",
                now=now,
            )
        else:
            task = create_task(
                session,
                channel_id="123",
                author_id="456",
                task_kind="digest",
                schedule_kind="cron",
                cron_expression=cron or "*/5 * * * *",
                digest_mode="summary",
                now=now,
            )
        assert isinstance(task, ScheduledTask)
        session.commit()
        return task.id, task.next_run_at


def test_one_shot_completion_is_atomic_with_occurrence_and_outbox(engine):
    task_id, scheduled_for = _insert_due(engine)
    now = datetime(2026, 9, 12, 12, 2, tzinfo=timezone.utc)
    with Session(engine) as session:
        claims = claim_due(session, now, "worker-a")
        session.commit()
    assert len(claims) == 1

    with Session(engine) as session:
        assert finalize_claim(session, claims[0], "rendered reminder", now)
        session.commit()
        task = session.get(ScheduledTask, task_id)
        occurrence = session.get(ScheduledTaskOccurrence, claims[0].occurrence_id)
        outbox = session.query(DiscordOutbox).one()
        assert task.status == "completed"
        assert occurrence.status == "enqueued"
        assert occurrence.scheduled_for == scheduled_for
        assert occurrence.outbox_id == outbox.id
        assert outbox.dedupe_key == f"scheduled:{claims[0].occurrence_id}"


def test_recurring_digest_advances_past_drain_time(engine):
    task_id, scheduled_for = _insert_due(engine, kind="digest")
    now = datetime(2026, 9, 12, 12, 6, tzinfo=timezone.utc)
    with Session(engine) as session:
        claim = claim_due(session, now, "worker-a")[0]
        session.commit()
    assert claim.scheduled_for == scheduled_for.replace(tzinfo=timezone.utc)

    with Session(engine) as session:
        assert finalize_claim(session, claim, "digest", now)
        session.commit()
        task = session.get(ScheduledTask, task_id)
        assert task.status == "pending"
        assert task.next_run_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 9, 12, 12, 10, tzinfo=timezone.utc
        )
        assert session.query(ScheduledTaskOccurrence).count() == 1


def test_concurrent_claimers_only_receive_one_occurrence(engine):
    _insert_due(engine)
    now = datetime(2026, 9, 12, 12, 2, tzinfo=timezone.utc)
    barrier = Barrier(2)

    def worker(name):
        with Session(engine) as session:
            barrier.wait()
            rows = claim_due(session, now, name)
            session.commit()
            return rows

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["worker-a", "worker-b"]))
    claims = [claim for result in results for claim in result]
    assert len(claims) == 1
    with Session(engine) as session:
        assert session.query(ScheduledTaskOccurrence).count() == 1


def test_restart_reclaims_stale_claim_with_same_occurrence_identity(engine):
    _insert_due(engine)
    first_now = datetime(2026, 9, 12, 12, 2, tzinfo=timezone.utc)
    with Session(engine) as session:
        first = claim_due(session, first_now, "dead-worker")[0]
        session.commit()
    with Session(engine) as session:
        assert (
            claim_due(session, first_now + timedelta(seconds=299), "new-worker") == []
        )
        session.rollback()
    with Session(engine) as session:
        recovered = claim_due(
            session, first_now + timedelta(seconds=301), "new-worker"
        )[0]
        session.commit()
    assert recovered.occurrence_id == first.occurrence_id
    assert recovered.claim_token != first.claim_token
    with Session(engine) as session:
        assert session.query(ScheduledTaskOccurrence).count() == 1


def test_duplicate_finalize_is_suppressed(engine):
    _insert_due(engine)
    now = datetime(2026, 9, 12, 12, 2, tzinfo=timezone.utc)
    with Session(engine) as session:
        claim = claim_due(session, now, "worker-a")[0]
        session.commit()
    with Session(engine) as session:
        assert finalize_claim(session, claim, "once", now)
        session.commit()
    with Session(engine) as session:
        assert not finalize_claim(session, claim, "twice", now)
        session.commit()
        assert session.query(DiscordOutbox).count() == 1


def test_generation_failure_releases_for_same_occurrence_retry(engine):
    _insert_due(engine)
    now = datetime(2026, 9, 12, 12, 2, tzinfo=timezone.utc)
    with Session(engine) as session:
        first = claim_due(session, now, "worker-a")[0]
        assert release_claim(session, first, "model unavailable")
        session.commit()
    with Session(engine) as session:
        retry = claim_due(session, now + timedelta(seconds=1), "worker-b")[0]
        session.commit()
        occurrence = session.get(ScheduledTaskOccurrence, retry.occurrence_id)
        assert occurrence.status == "claimed"
        assert retry.occurrence_id == first.occurrence_id
        assert session.get(ScheduledTask, retry.task_id).failure_count == 1


def test_one_shot_generation_failure_stops_after_retry_budget(engine):
    task_id, _ = _insert_due(engine)
    now = datetime(2026, 9, 12, 12, 2, tzinfo=timezone.utc)

    for attempt in range(MAX_GENERATION_ATTEMPTS):
        with Session(engine) as session:
            claims = claim_due(
                session, now + timedelta(seconds=attempt), f"worker-{attempt}"
            )
            assert len(claims) == 1
            assert release_claim(session, claims[0], "deterministic failure", now=now)
            session.commit()

    with Session(engine) as session:
        task = session.get(ScheduledTask, task_id)
        assert task.status == "failed"
        assert task.failure_count == MAX_GENERATION_ATTEMPTS
        assert claim_due(session, now + timedelta(minutes=1), "worker-final") == []


@pytest.mark.asyncio
async def test_proactive_drain_enqueues_delivery(engine):
    task_id, _ = _insert_due(engine)
    delivered = await drain_once(
        engine,
        now=datetime(2026, 9, 12, 12, 2, tzinfo=timezone.utc),
        claimant="leader-a",
    )
    assert delivered == 1
    with Session(engine) as session:
        task = session.get(ScheduledTask, task_id)
        outbox = session.query(DiscordOutbox).one()
        assert task.status == "completed"
        assert outbox.channel_id == "123"
        assert outbox.content == "⏰ <@456> reminder: stand up"

    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock())
    bot = MagicMock()
    bot.get_channel.return_value = channel
    assert await discord_outbox.drain_once(bot, engine) == 1
    with Session(engine) as session:
        occurrence = session.query(ScheduledTaskOccurrence).one()
        assert occurrence.status == "delivered"


@pytest.mark.asyncio
async def test_recurring_digest_builder_is_wired_to_outbox(engine):
    _insert_due(engine, kind="digest")
    calls = []

    async def build(_engine, claim):
        calls.append(claim.occurrence_id)
        return "📋 Scheduled summary digest:\n(window: 2 messages)\nDone"

    delivered = await drain_once(
        engine,
        now=datetime(2026, 9, 12, 12, 6, tzinfo=timezone.utc),
        claimant="leader-a",
        digest_builder=build,
    )
    assert delivered == 1
    assert len(calls) == 1
    with Session(engine) as session:
        outbox = session.query(DiscordOutbox).one()
        assert outbox.content.startswith("📋 Scheduled summary digest:")


@pytest.mark.asyncio
async def test_long_digest_is_clipped_before_enqueue(engine):
    _insert_due(engine, kind="digest")

    async def build(_engine, _claim):
        return "📋 Scheduled summary digest:\n" + "x" * 2_100

    assert (
        await drain_once(
            engine,
            now=datetime(2026, 9, 12, 12, 6, tzinfo=timezone.utc),
            claimant="leader-a",
            digest_builder=build,
        )
        == 1
    )
    with Session(engine) as session:
        content = session.query(DiscordOutbox).one().content
        assert len(content) == 2_000
        assert content.endswith("... (truncated)")


@pytest.mark.asyncio
async def test_repeated_digest_failure_advances_after_retry_budget(engine):
    task_id, scheduled_for = _insert_due(engine, kind="digest")
    now = datetime(2026, 9, 12, 12, 6, tzinfo=timezone.utc)
    calls = 0

    async def fail(_engine, _claim):
        nonlocal calls
        calls += 1
        raise RuntimeError("model unavailable")

    for _ in range(MAX_GENERATION_ATTEMPTS + 1):
        assert (
            await drain_once(
                engine,
                now=now,
                claimant="leader-a",
                digest_builder=fail,
            )
            == 0
        )

    assert calls == MAX_GENERATION_ATTEMPTS
    with Session(engine) as session:
        task = session.get(ScheduledTask, task_id)
        occurrence = session.query(ScheduledTaskOccurrence).one()
        assert occurrence.scheduled_for == scheduled_for
        assert occurrence.status == "failed"
        assert task.status == "pending"
        assert task.failure_count == 0
        assert task.next_run_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 9, 12, 12, 10, tzinfo=timezone.utc
        )


def test_validation_and_owner_scoping(engine):
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        assert "future" in create_task(
            session,
            channel_id="123",
            author_id="456",
            task_kind="reminder",
            schedule_kind="one_shot",
            due_at=now,
            content="late",
            now=now,
        )
        assert "five fields" in create_task(
            session,
            channel_id="123",
            author_id="456",
            task_kind="digest",
            schedule_kind="cron",
            cron_expression="bad cron",
            now=now,
        )
        task = create_task(
            session,
            channel_id="authorized-channel",
            author_id="owner",
            task_kind="digest",
            schedule_kind="cron",
            cron_expression="0 9 * * 1-5",
            now=now,
        )
        assert isinstance(task, ScheduledTask)
        session.commit()
        assert list_tasks(session, "other") == []
        assert not cancel_task(session, "other", task.id)
        assert cancel_task(session, "owner", task.id)
        session.commit()
        assert session.get(ScheduledTask, task.id).channel_id == "authorized-channel"


def test_model_constraints_reject_invalid_schedule_shape(engine):
    with Session(engine) as session:
        session.add(
            ScheduledTask(
                channel_id="123",
                author_id="456",
                task_kind="reminder",
                schedule_kind="one_shot",
                payload_json='{"content": "missing due_at"}',
                next_run_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_cron_semantics_use_standard_dom_dow_or_and_utc():
    after = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    # The 13th is a Sunday, so it matches even though day-of-month 1 does not.
    assert next_cron_at("0 9 1 * 0", after) == datetime(
        2026, 9, 13, 9, 0, tzinfo=timezone.utc
    )
