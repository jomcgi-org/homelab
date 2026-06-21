"""Unit tests for chat_public.sessions lifecycle functions (ADR 005).

Covers functions that have zero direct test coverage as of commits 05983b4..41c73a9:

  hash_value        -- pure function, None/empty -> None, salt prepended before hash
  _as_utc           -- None passthrough, naive gets UTC tzinfo, aware passes through
  _is_expired       -- TTL boundary (SESSION_TTL_SECONDS from limits)
  load_active_session -- None/empty id, missing row, non-active, expired, valid
  touch             -- bumps last_seen_at
  record_turn       -- increments turn_count, accumulates total_tokens, refreshes last_seen_at
  get_transcript    -- ordered by id, empty list for no messages
  compact_if_needed -- below trigger passthrough, above trigger calls summarize + persists

Follows the SQLite fixture pattern from router_test.py / phase2_test.py
(schema-stripping + SQLModel.metadata.create_all, StaticPool).
SQLite round-trips datetimes as naive, so we assert isinstance(v, datetime) and
never v.tzinfo per the monolith CLAUDE.md SQLite rule.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat_public import limits, sessions
from chat_public.models import ChatMessage, ChatSession


# ---------------------------------------------------------------------------
# Shared fixture: in-memory SQLite session (schema-stripped for SQLite compat)
# ---------------------------------------------------------------------------


@pytest.fixture(name="db")
def db_fixture():
    """In-memory SQLite session with schema annotations stripped."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas: dict[str, str] = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as db:
            yield db
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


# ---------------------------------------------------------------------------
# Helper: persist a session row directly (no endpoint needed)
# ---------------------------------------------------------------------------


def _make_session(db: Session, **overrides) -> ChatSession:
    row = sessions.create_session(db)
    for key, value in overrides.items():
        setattr(row, key, value)
    if overrides:
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _make_message(
    db: Session, session: ChatSession, role: str = "user", content: str = "hi"
) -> ChatMessage:
    return sessions.append_message(db, session, role=role, content=content)


# ---------------------------------------------------------------------------
# 1. hash_value
# ---------------------------------------------------------------------------


class TestHashValue:
    def test_none_returns_none(self):
        assert sessions.hash_value(None) is None

    def test_empty_string_returns_none(self):
        assert sessions.hash_value("") is None

    def test_non_empty_returns_sha256_hex(self):
        result = sessions.hash_value("192.0.2.1")
        expected = hashlib.sha256("192.0.2.1".encode("utf-8")).hexdigest()
        assert result == expected
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_salt_prepended_before_hashing(self):
        value = "192.0.2.1"
        salt = "mysalt"
        result = sessions.hash_value(value, salt)
        expected = hashlib.sha256((salt + value).encode("utf-8")).hexdigest()
        assert result == expected

    def test_salt_changes_output(self):
        value = "192.0.2.1"
        without_salt = sessions.hash_value(value, "")
        with_salt = sessions.hash_value(value, "extra")
        assert without_salt != with_salt

    def test_empty_salt_matches_unsalted_sha256(self):
        value = "test-ua"
        result = sessions.hash_value(value, "")
        expected = hashlib.sha256(value.encode("utf-8")).hexdigest()
        assert result == expected


# ---------------------------------------------------------------------------
# 2. _as_utc
# ---------------------------------------------------------------------------


class TestAsUtc:
    def test_none_returns_none(self):
        assert sessions._as_utc(None) is None

    def test_naive_datetime_gets_utc_tzinfo(self):
        naive = datetime(2025, 1, 15, 12, 0, 0)
        assert naive.tzinfo is None
        result = sessions._as_utc(naive)
        assert isinstance(result, datetime)
        # After _as_utc the tzinfo is UTC -- check via utcoffset
        assert result.utcoffset() == timedelta(0)

    def test_naive_datetime_preserves_wall_time(self):
        naive = datetime(2025, 6, 1, 9, 30, 45)
        result = sessions._as_utc(naive)
        assert result.year == 2025
        assert result.month == 6
        assert result.day == 1
        assert result.hour == 9
        assert result.minute == 30
        assert result.second == 45

    def test_aware_datetime_passes_through_unchanged(self):
        aware = datetime(2025, 3, 10, 8, 0, 0, tzinfo=timezone.utc)
        result = sessions._as_utc(aware)
        assert result is aware


# ---------------------------------------------------------------------------
# 3. _is_expired
# ---------------------------------------------------------------------------


class TestIsExpired:
    def _session_with_last_seen(self, db: Session, delta_seconds: int) -> ChatSession:
        """A session whose last_seen_at is delta_seconds ago from now."""
        row = _make_session(db)
        past = datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)
        row.last_seen_at = past
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def test_not_expired_within_ttl(self, db):
        row = self._session_with_last_seen(db, limits.SESSION_TTL_SECONDS - 60)
        now = datetime.now(timezone.utc)
        assert sessions._is_expired(row, now) is False

    def test_expired_beyond_ttl(self, db):
        row = self._session_with_last_seen(db, limits.SESSION_TTL_SECONDS + 1)
        now = datetime.now(timezone.utc)
        assert sessions._is_expired(row, now) is True

    def test_exactly_at_ttl_boundary_is_expired(self, db):
        # now - last_seen == SESSION_TTL_SECONDS: timedelta > timedelta is False,
        # but == SESSION_TTL_SECONDS is not strictly greater, so NOT expired.
        # The boundary check is `now - last_seen > TTL`, meaning exactly equal is
        # still within the window. Verify the boundary math.
        ttl = timedelta(seconds=limits.SESSION_TTL_SECONDS)
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        last_seen = now - ttl
        row = _make_session(db)
        row.last_seen_at = last_seen
        db.add(row)
        db.commit()
        db.refresh(row)
        # Exactly at TTL: not yet expired (> not >=)
        assert sessions._is_expired(row, now) is False

    def test_one_second_over_ttl_boundary_is_expired(self, db):
        ttl = timedelta(seconds=limits.SESSION_TTL_SECONDS)
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        last_seen = now - ttl - timedelta(seconds=1)
        row = _make_session(db)
        row.last_seen_at = last_seen
        db.add(row)
        db.commit()
        db.refresh(row)
        assert sessions._is_expired(row, now) is True


# ---------------------------------------------------------------------------
# 4. load_active_session
# ---------------------------------------------------------------------------


class TestLoadActiveSession:
    def test_none_session_id_returns_none(self, db):
        assert sessions.load_active_session(db, None) is None

    def test_empty_string_session_id_returns_none(self, db):
        assert sessions.load_active_session(db, "") is None

    def test_missing_row_returns_none(self, db):
        assert sessions.load_active_session(db, "does-not-exist") is None

    def test_non_active_status_returns_none(self, db):
        row = _make_session(db, status="expired")
        assert sessions.load_active_session(db, row.id) is None

    def test_purged_status_returns_none(self, db):
        row = _make_session(db, status="purged")
        assert sessions.load_active_session(db, row.id) is None

    def test_expired_session_flipped_to_expired_and_returns_none(
        self, db, monkeypatch
    ):
        # Place last_seen_at well past the TTL so _is_expired returns True.
        stale = datetime.now(timezone.utc) - timedelta(
            seconds=limits.SESSION_TTL_SECONDS + 3600
        )
        row = _make_session(db, last_seen_at=stale)
        assert row.status == "active"

        result = sessions.load_active_session(db, row.id)

        assert result is None
        db.refresh(row)
        assert row.status == "expired"

    def test_valid_active_session_returned(self, db):
        row = _make_session(db)
        result = sessions.load_active_session(db, row.id)
        assert result is not None
        assert result.id == row.id
        assert result.status == "active"

    def test_missing_and_expired_are_indistinguishable(self, db):
        """Expiry and absence both return None: no information leak."""
        stale = datetime.now(timezone.utc) - timedelta(
            seconds=limits.SESSION_TTL_SECONDS + 3600
        )
        row = _make_session(db, last_seen_at=stale)

        expired_result = sessions.load_active_session(db, row.id)
        missing_result = sessions.load_active_session(db, "totally-fake-id")

        assert expired_result is None
        assert missing_result is None


# ---------------------------------------------------------------------------
# 5. touch
# ---------------------------------------------------------------------------


class TestTouch:
    def test_touch_bumps_last_seen_at(self, db):
        # Set last_seen_at to a known past time.
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        row = _make_session(db, last_seen_at=past)

        sessions.touch(db, row)

        db.refresh(row)
        updated = row.last_seen_at
        assert isinstance(updated, datetime)
        # The updated value must be strictly later than the past timestamp.
        # Coerce to UTC for comparison since SQLite returns naive datetimes.
        updated_utc = updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)
        past_utc = past if past.tzinfo else past.replace(tzinfo=timezone.utc)
        assert updated_utc > past_utc

    def test_touch_does_not_change_turn_count_or_tokens(self, db):
        row = _make_session(db)
        sessions.touch(db, row)
        db.refresh(row)
        assert row.turn_count == 0
        assert row.total_tokens == 0


# ---------------------------------------------------------------------------
# 6. record_turn
# ---------------------------------------------------------------------------


class TestRecordTurn:
    def test_increments_turn_count_by_one(self, db):
        row = _make_session(db)
        assert row.turn_count == 0
        sessions.record_turn(db, row, tokens=100)
        db.refresh(row)
        assert row.turn_count == 1

    def test_increments_turn_count_cumulatively(self, db):
        row = _make_session(db)
        sessions.record_turn(db, row, tokens=10)
        sessions.record_turn(db, row, tokens=20)
        db.refresh(row)
        assert row.turn_count == 2

    def test_adds_tokens_to_total_tokens(self, db):
        row = _make_session(db)
        sessions.record_turn(db, row, tokens=150)
        db.refresh(row)
        assert row.total_tokens == 150

    def test_accumulates_tokens_across_turns(self, db):
        row = _make_session(db)
        sessions.record_turn(db, row, tokens=100)
        sessions.record_turn(db, row, tokens=200)
        db.refresh(row)
        assert row.total_tokens == 300

    def test_refreshes_last_seen_at(self, db):
        past = datetime.now(timezone.utc) - timedelta(minutes=10)
        row = _make_session(db, last_seen_at=past)

        sessions.record_turn(db, row, tokens=50)

        db.refresh(row)
        updated = row.last_seen_at
        assert isinstance(updated, datetime)
        updated_utc = (
            updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)
        )
        past_utc = past if past.tzinfo else past.replace(tzinfo=timezone.utc)
        assert updated_utc > past_utc

    def test_zero_tokens_still_increments_turn_count(self, db):
        row = _make_session(db)
        sessions.record_turn(db, row, tokens=0)
        db.refresh(row)
        assert row.turn_count == 1
        assert row.total_tokens == 0


# ---------------------------------------------------------------------------
# 7. get_transcript
# ---------------------------------------------------------------------------


class TestGetTranscript:
    def test_empty_transcript_returns_empty_list(self, db):
        row = _make_session(db)
        result = sessions.get_transcript(db, row)
        assert result == []

    def test_returns_all_messages(self, db):
        row = _make_session(db)
        _make_message(db, row, "user", "first")
        _make_message(db, row, "assistant", "second")
        result = sessions.get_transcript(db, row)
        assert len(result) == 2

    def test_messages_ordered_by_id_oldest_first(self, db):
        row = _make_session(db)
        _make_message(db, row, "user", "first")
        _make_message(db, row, "assistant", "second")
        _make_message(db, row, "user", "third")
        result = sessions.get_transcript(db, row)
        assert [m.content for m in result] == ["first", "second", "third"]
        # IDs must be strictly ascending
        ids = [m.id for m in result]
        assert ids == sorted(ids)

    def test_only_returns_messages_for_the_given_session(self, db):
        row_a = _make_session(db)
        row_b = _make_session(db)
        _make_message(db, row_a, "user", "for-a")
        _make_message(db, row_b, "user", "for-b")

        result_a = sessions.get_transcript(db, row_a)
        result_b = sessions.get_transcript(db, row_b)

        assert len(result_a) == 1
        assert result_a[0].content == "for-a"
        assert len(result_b) == 1
        assert result_b[0].content == "for-b"


# ---------------------------------------------------------------------------
# 8. compact_if_needed (async)
# ---------------------------------------------------------------------------


class TestCompactIfNeeded:
    @pytest.mark.asyncio
    async def test_below_trigger_returns_unchanged(self, db):
        """When estimated tokens are well below the trigger, passthrough."""
        row = _make_session(db)
        transcript = [
            ChatMessage(session_id=row.id, role="user", content="hi"),
        ]
        # One tiny message will not cross the compaction threshold.
        summarize = AsyncMock(return_value="should not be called")

        summary, tail = await sessions.compact_if_needed(
            db, row, transcript, summarize=summarize
        )

        summarize.assert_not_called()
        assert summary is None  # no rolling_summary yet
        assert tail is transcript

    @pytest.mark.asyncio
    async def test_below_trigger_preserves_existing_summary(self, db):
        row = _make_session(db, rolling_summary="Old summary")
        transcript = [ChatMessage(session_id=row.id, role="user", content="hi")]
        summarize = AsyncMock()

        summary, tail = await sessions.compact_if_needed(
            db, row, transcript, summarize=summarize
        )

        summarize.assert_not_called()
        assert summary == "Old summary"
        assert tail is transcript

    @pytest.mark.asyncio
    async def test_above_trigger_calls_summarize(self, db, monkeypatch):
        """When estimated context crosses the trigger, summarize is called."""
        monkeypatch.setattr(limits, "should_compact", lambda _: True)
        # Need more messages than COMPACTION_KEEP_MESSAGES so the older set is non-empty.
        row = _make_session(db)
        keep = limits.COMPACTION_KEEP_MESSAGES
        transcript = [
            ChatMessage(session_id=row.id, role="user", content=f"msg {i}")
            for i in range(keep + 3)
        ]
        summarize = AsyncMock(return_value="New rolled summary")

        summary, tail = await sessions.compact_if_needed(
            db, row, transcript, summarize=summarize
        )

        summarize.assert_called_once()
        assert summary == "New rolled summary"

    @pytest.mark.asyncio
    async def test_above_trigger_persists_rolling_summary(self, db, monkeypatch):
        monkeypatch.setattr(limits, "should_compact", lambda _: True)
        row = _make_session(db)
        keep = limits.COMPACTION_KEEP_MESSAGES
        transcript = [
            ChatMessage(session_id=row.id, role="user", content=f"msg {i}")
            for i in range(keep + 2)
        ]
        summarize = AsyncMock(return_value="Persisted summary")

        await sessions.compact_if_needed(db, row, transcript, summarize=summarize)

        db.refresh(row)
        assert row.rolling_summary == "Persisted summary"

    @pytest.mark.asyncio
    async def test_above_trigger_returns_recent_tail_only(self, db, monkeypatch):
        monkeypatch.setattr(limits, "should_compact", lambda _: True)
        row = _make_session(db)
        keep = limits.COMPACTION_KEEP_MESSAGES
        total = keep + 4
        transcript = [
            ChatMessage(session_id=row.id, role="user", content=f"msg {i}")
            for i in range(total)
        ]
        summarize = AsyncMock(return_value="Tail test summary")

        _, tail = await sessions.compact_if_needed(
            db, row, transcript, summarize=summarize
        )

        # Only the most-recent `keep` messages survive verbatim.
        assert len(tail) == keep
        assert tail == transcript[-keep:]

    @pytest.mark.asyncio
    async def test_above_trigger_summarize_receives_older_messages(
        self, db, monkeypatch
    ):
        monkeypatch.setattr(limits, "should_compact", lambda _: True)
        row = _make_session(db, rolling_summary="Prior summary")
        keep = limits.COMPACTION_KEEP_MESSAGES
        transcript = [
            ChatMessage(session_id=row.id, role="user", content=f"msg {i}")
            for i in range(keep + 3)
        ]
        captured: dict = {}

        async def _fake_summarize(existing_summary, older):
            captured["existing"] = existing_summary
            captured["older"] = older
            return "Updated summary"

        await sessions.compact_if_needed(
            db, row, transcript, summarize=_fake_summarize
        )

        # Summarize gets the existing rolling summary and only the older messages.
        assert captured["existing"] == "Prior summary"
        assert captured["older"] == transcript[:-keep]

    @pytest.mark.asyncio
    async def test_short_transcript_skips_even_when_should_compact_true(
        self, db, monkeypatch
    ):
        """If transcript <= keep, we skip even when the token estimate is high."""
        monkeypatch.setattr(limits, "should_compact", lambda _: True)
        row = _make_session(db)
        keep = limits.COMPACTION_KEEP_MESSAGES
        # Transcript has exactly `keep` messages -- boundary: should skip.
        transcript = [
            ChatMessage(session_id=row.id, role="user", content="x" * 100)
            for _ in range(keep)
        ]
        summarize = AsyncMock(return_value="should not appear")

        summary, tail = await sessions.compact_if_needed(
            db, row, transcript, summarize=summarize
        )

        summarize.assert_not_called()
        assert tail is transcript
