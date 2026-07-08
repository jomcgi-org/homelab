"""SQLModel definitions for the dr_jobs schema (NHS Scotland vacancy aggregator).

Mirrors chart/migrations/20260616130000_dr_jobs_schema.sql.

One row per NHS JobTrain vacancy on apply.jobs.scot.nhs.uk, keyed by the numeric
JobId from the listing. The daily scrape upserts the structured JSON-LD fields
and stamps last_seen_at. Rows are never deleted (Option A lifecycle): a closed
posting drops out of the live view (which filters on last_seen_at + closing_date)
but stays queryable for the history view.
"""

from datetime import date, datetime, timezone

from sqlmodel import Field, SQLModel


class Vacancy(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "nhs_vacancies"
    __table_args__ = {"schema": "dr_jobs", "extend_existing": True}

    # The numeric JobTrain JobId (e.g. "256437"); stable across re-scrapes.
    job_id: str = Field(primary_key=True)
    # Leading board reference parsed from the title (e.g. "PS246039"); "" if none.
    reference: str = Field(default="")
    title: str
    employment_type: str = Field(default="")
    # Salary band name ("Consultant" / "Locum Consultant") split off salary_text.
    salary_band: str = Field(default="")
    salary_text: str = Field(default="")
    town: str = Field(default="")
    postcode: str = Field(default="")
    region: str = Field(default="")
    posted_date: date | None = None
    closing_date: date | None = None
    url: str
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
