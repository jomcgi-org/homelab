"""Unit tests for campsites.jobs pure DB adapters (no network).

The campsites SQLModel tables are schema-qualified (schema="campsites"), which
SQLite has no concept of, so we strip the schema for the test and recreate the
tables on an in-memory SQLite engine, mirroring worldcup/jobs_test.py and
dr_jobs/jobs_test.py. core.db.get_engine is monkeypatched at the test engine so
adapters that open their own Session land on it.

These tests exercise the sync upsert/prune helpers (_load_and_upsert_catalog,
_upsert_availability, _upsert_weather); refresh_handler's network phase is not
exercised here.
"""

from __future__ import annotations

import datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import core.db as app_db
import campsites.jobs as jobs
from campsites.client import CampgroundRow, DayAvail
from campsites.models import Availability, Campground, Weather
from campsites.weather import WxDay

_START = datetime.date(2026, 7, 1)
_YESTERDAY = _START - datetime.timedelta(days=1)


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
        monkeypatch.setattr(app_db, "get_engine", lambda: engine)
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _row(rid=-101):
    return CampgroundRow(
        resource_location_id=rid,
        park_map_id=202,
        name="Test Park",
        region="Cariboo",
        latitude=52.0,
        longitude=-122.0,
        iana_tz="America/Vancouver",
        description="",
        booking_url="https://bcparks.ca/test-park/",
    )


def _day_avail(offset, has=True, loops=2):
    return DayAvail(
        date=_START + datetime.timedelta(days=offset),
        has_availability=has,
        loops_open=loops,
    )


def _wx_day(offset, prob=40, score=88, good=True):
    return WxDay(
        date=_START + datetime.timedelta(days=offset),
        cloud_cover=12.0,
        precip_sum=0.0,
        precip_prob=prob,
        temp_max=22.0,
        wind_max=8.0,
        sunny_score=score,
        is_good=good,
    )


class TestLoadAndUpsertCatalog:
    def test_upserts_campgrounds_and_returns_rows(self, engine, monkeypatch):
        monkeypatch.setattr(
            jobs.client, "load_catalog", lambda: [_row(-101), _row(-102)]
        )
        rows = jobs._load_and_upsert_catalog()

        assert {r.resource_location_id for r in rows} == {-101, -102}
        with Session(engine) as session:
            stored = session.exec(select(Campground)).all()
            assert {c.resource_location_id for c in stored} == {-101, -102}
            one = session.get(Campground, -101)
            assert one.name == "Test Park"
            assert one.booking_url == "https://bcparks.ca/test-park/"
            assert one.updated_at is not None

    def test_rerun_updates_in_place(self, engine, monkeypatch):
        monkeypatch.setattr(jobs.client, "load_catalog", lambda: [_row(-101)])
        jobs._load_and_upsert_catalog()

        changed = _row(-101)
        changed.name = "Renamed Park"
        monkeypatch.setattr(jobs.client, "load_catalog", lambda: [changed])
        jobs._load_and_upsert_catalog()

        with Session(engine) as session:
            assert len(session.exec(select(Campground)).all()) == 1
            assert session.get(Campground, -101).name == "Renamed Park"


class TestUpsertAvailability:
    def test_writes_rows_and_prunes_past_window(self, engine):
        # Seed a stale (pre-window) row that the prune must remove.
        with Session(engine) as session:
            session.add(
                Availability(
                    resource_location_id=-101,
                    date=_YESTERDAY,
                    has_availability=True,
                    loops_open=1,
                )
            )
            session.commit()

        written = jobs._upsert_availability(
            {-101: [_day_avail(0), _day_avail(1, has=False, loops=0)]}, _START
        )
        assert written == 2

        with Session(engine) as session:
            rows = session.exec(select(Availability)).all()
            dates = {r.date for r in rows}
            assert _YESTERDAY not in dates  # pruned
            assert dates == {_START, _START + datetime.timedelta(days=1)}
            day0 = session.get(Availability, (-101, _START))
            assert day0.has_availability is True
            assert day0.loops_open == 2
            assert day0.scraped_at is not None

    def test_empty_input_still_prunes(self, engine):
        with Session(engine) as session:
            session.add(Availability(resource_location_id=-101, date=_YESTERDAY))
            session.commit()

        written = jobs._upsert_availability({}, _START)
        assert written == 0
        with Session(engine) as session:
            assert session.exec(select(Availability)).all() == []

    def test_rerun_upserts_without_duplicates(self, engine):
        jobs._upsert_availability({-101: [_day_avail(0, loops=1)]}, _START)
        jobs._upsert_availability({-101: [_day_avail(0, loops=3)]}, _START)
        with Session(engine) as session:
            rows = session.exec(select(Availability)).all()
            assert len(rows) == 1
            assert rows[0].loops_open == 3


class TestUpsertWeather:
    def test_writes_rows_prunes_and_coerces_prob_to_int(self, engine):
        with Session(engine) as session:
            session.add(Weather(resource_location_id=-101, date=_YESTERDAY))
            session.commit()

        written = jobs._upsert_weather(
            {-101: [_wx_day(0, prob=40, score=88, good=True)]}, _START
        )
        assert written == 1

        with Session(engine) as session:
            rows = session.exec(select(Weather)).all()
            assert {r.date for r in rows} == {_START}  # yesterday pruned
            wx = session.get(Weather, (-101, _START))
            assert wx.precip_prob == 40
            assert isinstance(wx.precip_prob, int)
            assert wx.sunny_score == 88
            assert wx.is_good is True
            assert wx.fetched_at is not None

    def test_none_precip_prob_survives(self, engine):
        day = _wx_day(0)
        day.precip_prob = None
        jobs._upsert_weather({-101: [day]}, _START)
        with Session(engine) as session:
            assert session.get(Weather, (-101, _START)).precip_prob is None
