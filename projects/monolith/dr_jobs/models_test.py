"""Unit tests for dr_jobs/models.py on SQLite (create_all, no migrations).

Mirrors hikes/models_test: strip the Postgres-only schema= override so
SQLModel.metadata.create_all() lands the table in SQLite's default schema, then
round-trip a row.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from dr_jobs.models import Vacancy


@pytest.fixture(name="session")
def session_fixture():
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
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


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
