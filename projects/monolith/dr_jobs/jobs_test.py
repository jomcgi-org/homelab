"""Unit tests for dr_jobs.jobs.

build_digest is pure (no DB/network). _persist is exercised against in-memory
SQLite by monkeypatching app.db.get_engine to the test engine (it opens its own
session from get_engine, mirroring hikes._persist_walks). Asserts the Option A
lifecycle: seed suppression, insert vs update accounting, and that an unseen
JobId is what counts as "new".
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import app.db as app_db
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
