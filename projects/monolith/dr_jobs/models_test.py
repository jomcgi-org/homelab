"""Unit tests for dr_jobs/models.py on SQLite (create_all, no migrations).

Mirrors hikes/models_test: strip the Postgres-only schema= override so
SQLModel.metadata.create_all() lands the table in SQLite's default schema, then
round-trip a row.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlmodel import Session

from dr_jobs.models import Vacancy
from shared.testing.sqlite import sqlite_session


@pytest.fixture(name="session")
def session_fixture():
    with sqlite_session() as session:
        yield session


def test_round_trip(session: Session):
    now = datetime.now(timezone.utc)
    session.add(
        Vacancy(
            job_id="256437",
            reference="PS246039",
            title="PS246039 - Consultant Anaesthetist",
            employment_type="Permanent",
            salary_band="Consultant",
            salary_text="Consultant (£111,430 - £148,064)",
            town="Elgin",
            postcode="IV30 1SN",
            posted_date=date(2026, 6, 10),
            closing_date=date(2026, 7, 12),
            url="https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId=256437",
            first_seen_at=now,
            last_seen_at=now,
            scraped_at=now,
        )
    )
    session.commit()

    row = session.get(Vacancy, "256437")
    assert row is not None
    assert row.reference == "PS246039"
    assert row.salary_band == "Consultant"
    assert row.closing_date == date(2026, 7, 12)
    assert isinstance(row.first_seen_at, datetime)


def test_defaults(session: Session):
    # Optional text fields default to "", dates to None.
    session.add(
        Vacancy(
            job_id="1",
            title="Consultant in Paediatric Anaesthesia",
            url="https://example.invalid/Job/JobDetail?JobId=1",
        )
    )
    session.commit()
    row = session.get(Vacancy, "1")
    assert row.reference == ""
    assert row.salary_band == ""
    assert row.posted_date is None
    assert row.closing_date is None


def test_active_model_has_only_discord_notification_state():
    assert "notified_whatsapp" not in Vacancy.model_fields
    assert {
        c.name for c in Vacancy.__table__.columns if c.name.startswith("notified_")
    } == {"notified_discord"}
