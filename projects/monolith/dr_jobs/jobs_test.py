"""Unit tests for dr_jobs.jobs.

build_digest is pure (no DB/network). _persist is exercised against in-memory
SQLite by monkeypatching core.db.get_engine to the test engine (it opens its own
session from get_engine, mirroring hikes._persist_walks). Asserts the Option A
lifecycle: seed suppression, insert vs update accounting, and that an unseen
JobId is what counts as "new".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import core.db as app_db
import dr_jobs.jobs as jobs
from dr_jobs.models import Vacancy


@pytest.fixture(name="engine")
def engine_fixture(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        # _persist opens Session(get_engine()); point it at the test engine.
        monkeypatch.setattr(app_db, "get_engine", lambda: engine)
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _vac(job_id, title="PS1 - Consultant Anaesthetist", closing=date(2026, 7, 12)):
    return {
        "job_id": job_id,
        "reference": "PS1",
        "title": title,
        "employment_type": "Permanent",
        "salary_band": "Consultant",
        "salary_text": "Consultant (£111,430 - £148,064)",
        "town": "Elgin",
        "postcode": "IV30 1SN",
        "region": "",
        "posted_date": date(2026, 6, 10),
        "closing_date": closing,
        "url": f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={job_id}",
    }


class TestPersist:
    def test_seed_run_inserts_but_flags_seed(self, engine):
        new, updated, was_seed = jobs._persist([_vac("A"), _vac("B")])
        assert was_seed is True  # table was empty
        assert {v["job_id"] for v in new} == {"A", "B"}
        assert updated == 0
        with Session(engine) as s:
            assert len(s.exec(select(Vacancy)).all()) == 2

    def test_second_run_detects_new_and_updates(self, engine):
        jobs._persist([_vac("A")])  # seed
        new, updated, was_seed = jobs._persist(
            [_vac("A", title="PS1 - Consultant Anaesthetist (amended)"), _vac("B")]
        )
        assert was_seed is False
        assert [v["job_id"] for v in new] == ["B"]  # only B is unseen
        assert updated == 1  # A refreshed
        with Session(engine) as s:
            a = s.get(Vacancy, "A")
            assert a.title.endswith("(amended)")

    def test_first_seen_preserved_on_update(self, engine):
        jobs._persist([_vac("A")])
        with Session(engine) as s:
            first_seen = s.get(Vacancy, "A").first_seen_at
        jobs._persist([_vac("A")])
        with Session(engine) as s:
            assert s.get(Vacancy, "A").first_seen_at == first_seen


def _insert(engine, job_id, *, closing, notified_discord=None, notified_whatsapp=None):
    """Insert a single vacancy with explicit notification state (bypasses the
    seed logic so a row can be born pending)."""
    with Session(engine) as s:
        v = Vacancy(**_vac(job_id, closing=closing))
        v.notified_discord = notified_discord
        v.notified_whatsapp = notified_whatsapp
        s.add(v)
        s.commit()


@pytest.fixture(name="captured")
def captured_fixture(monkeypatch):
    """Capture the direct-to-outbox enqueue calls _notify_pending_sync makes.

    The function does ``from chat.api import ...`` at call time, so patching the
    chat.api attributes (not a local name) is what the local import picks up.
    """
    calls = {"discord": [], "whatsapp": []}
    monkeypatch.setattr(
        "chat.api.enqueue_message",
        lambda session, channel_id, *, content=None, **_: calls["discord"].append(
            (channel_id, content)
        ),
    )
    monkeypatch.setattr(
        "chat.api.enqueue_whatsapp_message",
        lambda jid, content: calls["whatsapp"].append((jid, content)),
    )
    monkeypatch.setattr("chat.api.whatsapp_household_group_jids", lambda: ["g@g.us"])
    return calls


class TestSeedStamping:
    def test_seed_rows_born_notified(self, engine):
        # Every row on an empty-table seed is stamped so the switchover cannot
        # dump the backlog into the chat.
        jobs._persist([_vac("A")])
        with Session(engine) as s:
            a = s.get(Vacancy, "A")
            assert a.notified_discord is not None
            assert a.notified_whatsapp is not None

    def test_nonseed_new_row_left_pending(self, engine):
        jobs._persist([_vac("SEED")])  # seed run stamps SEED
        jobs._persist([_vac("SEED"), _vac("A")])  # A is a new, non-seed insert
        with Session(engine) as s:
            a = s.get(Vacancy, "A")
            assert a.notified_discord is None
            assert a.notified_whatsapp is None


class TestNotifyPending:
    def test_enqueues_and_stamps_open_pending(self, engine, captured):
        _insert(engine, "A", closing=date.today() + timedelta(days=5))
        jobs._notify_pending_sync()
        assert len(captured["discord"]) == 1
        channel, content = captured["discord"][0]
        assert channel == jobs.DISCORD_CHANNEL_ID
        assert "1 new NHS Scotland" in content
        assert captured["whatsapp"] == [("g@g.us", content)]
        with Session(engine) as s:
            a = s.get(Vacancy, "A")
            assert a.notified_discord is not None
            assert a.notified_whatsapp is not None

    def test_skips_closed_vacancy(self, engine, captured):
        _insert(engine, "A", closing=date.today() - timedelta(days=1))
        jobs._notify_pending_sync()
        assert captured["discord"] == []
        assert captured["whatsapp"] == []
        with Session(engine) as s:
            assert s.get(Vacancy, "A").notified_discord is None

    def test_does_not_renotify_already_stamped(self, engine, captured):
        stamp = datetime.now(timezone.utc)
        _insert(
            engine,
            "A",
            closing=date.today() + timedelta(days=5),
            notified_discord=stamp,
            notified_whatsapp=stamp,
        )
        jobs._notify_pending_sync()
        assert captured["discord"] == []
        assert captured["whatsapp"] == []

    def test_whatsapp_no_group_leaves_it_pending(self, engine, captured, monkeypatch):
        # Discord still fires; WhatsApp stays NULL until a group is configured.
        monkeypatch.setattr("chat.api.whatsapp_household_group_jids", lambda: [])
        _insert(engine, "A", closing=date.today() + timedelta(days=5))
        jobs._notify_pending_sync()
        assert len(captured["discord"]) == 1
        assert captured["whatsapp"] == []
        with Session(engine) as s:
            a = s.get(Vacancy, "A")
            assert a.notified_discord is not None
            assert a.notified_whatsapp is None

    def test_discord_failure_isolated_and_retryable(
        self, engine, captured, monkeypatch
    ):
        def boom(*_a, **_k):
            raise RuntimeError("outbox down")

        monkeypatch.setattr("chat.api.enqueue_message", boom)
        _insert(engine, "A", closing=date.today() + timedelta(days=5))
        jobs._notify_pending_sync()  # must not raise
        with Session(engine) as s:
            a = s.get(Vacancy, "A")
            assert a.notified_discord is None  # left NULL -> retried next run
            assert a.notified_whatsapp is not None  # WhatsApp still delivered
        assert len(captured["whatsapp"]) == 1


class TestBuildDigest:
    def test_lists_jobs_with_count_and_link(self):
        msg = jobs.build_digest([_vac("A", title="Consultant Anaesthetist")])
        assert "1 new NHS Scotland anaesthetics consultant job:" in msg
        assert "• Consultant Anaesthetist · Elgin, closes 12 Jul" in msg
        assert "https://jomcgi.dev/app/dr-jobs" in msg

    def test_plural_and_no_em_dash(self):
        msg = jobs.build_digest([_vac("A"), _vac("B")])
        assert "2 new NHS Scotland anaesthetics consultant jobs:" in msg
        assert "—" not in msg  # never an em-dash (house style)

    def test_caps_long_batches(self):
        many = [_vac(str(i), title=f"Job {i}") for i in range(20)]
        msg = jobs.build_digest(many)
        assert "...and 8 more" in msg

    def test_missing_closing_date(self):
        msg = jobs.build_digest([_vac("A", closing=None)])
        assert "no closing date" in msg
